# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-节点管理(BlueKing-BK-NODEMAN) available.
Copyright (C) 2017-2022 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List

import django_filters
from django.db.models import QuerySet
from django.http import JsonResponse
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
from drf_yasg import openapi
from rest_framework import filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK

from apps.backend.sync_task.constants import SyncTaskType
from apps.core.files.storage import get_storage
from apps.core.ipchooser.tools.base import HostQuerySqlHelper
from apps.core.tag.constants import TargetType
from apps.core.tag.models import Tag
from apps.generic import ApiMixinModelViewSet as ModelViewSet
from apps.generic import ValidationMixin
from apps.node_man import constants, exceptions, models
from apps.node_man.constants import CategoryType
from apps.node_man.handlers.gse_package import gse_package_handler
from apps.node_man.models import GsePackageDesc, GsePackages, UploadPackage
from apps.node_man.permissions import package_manage as pkg_permission
from apps.node_man.serializers import package_manage as pkg_manage
from apps.node_man.tools.package import PackageTools
from apps.utils.local import get_request_username
from common.api import NodeApi
from common.utils.drf_utils import swagger_auto_schema

PACKAGE_MANAGE_VIEW_TAGS = ["PKG_Manager"]
PACKAGE_DES_VIEW_TAGS = ["PKG_Desc"]
logger = logging.getLogger("app")


class GsePackageFilter(FilterSet):
    os = django_filters.BaseInFilter(field_name="os", lookup_expr="in")
    cpu_arch = django_filters.BaseInFilter(field_name="cpu_arch", lookup_expr="in")
    tag_names = django_filters.BaseInFilter(lookup_expr="in", method="filter_tag_names")
    created_by = django_filters.BaseInFilter(field_name="created_by", lookup_expr="in")
    is_ready = django_filters.BaseInFilter(field_name="is_ready", lookup_expr="in")
    version = django_filters.BaseInFilter(field_name="version", lookup_expr="in")
    created_time = django_filters.DateTimeFromToRangeFilter()

    def filter_tag_names(self, queryset, name, tag_names):
        # 筛选标签必须带上project筛选条件，否则会出现数据和预期不一致的情况
        return gse_package_handler.filter_tags(queryset, self.request.query_params.get("project"), tag_names=tag_names)

    class Meta:
        model = GsePackages
        fields = ["tag_names", "project", "created_by", "is_ready", "version", "os", "cpu_arch"]


class PackageManageViewSet(ValidationMixin, ModelViewSet):
    serializer_class = pkg_manage.PackageSerializer
    permission_classes = (pkg_permission.PackageManagePermission,)
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filter_class = GsePackageFilter
    ordering_fields = ["version", "created_time"]

    def get_queryset(self):
        return models.GsePackages.objects.all()

    @swagger_auto_schema(
        responses={200: pkg_manage.ListResponseSerializer},
        operation_summary="安装包列表",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    def list(self, request, *args, **kwargs):
        """
        return: {
            "total": 2,
            "list": [
                {
                    "id": 1,
                    "pkg_name": "pkg_name",
                    "version": "1.1.1",
                    "os": "Linux",
                    "cpu_arch": "x86_64",
                    "tags": [{"id": "stable", "name": "稳定版本"}],
                    "creator": "string",
                    "pkg_ctime": "2019-08-24 14:15:22",
                    "is_ready": True,
                },
                {
                    "id": 2,
                    "pkg_name": "pkg_name",
                    "version": "1.1.2",
                    "os": "Linux",
                    "os_cpu_arch": "x86_64",
                    "tags": [{"id": "stable", "name": "稳定版本"}],
                    "creator": "string",
                    "pkg_ctime": "2019-08-24 14:15:22",
                    "is_ready": True,
                },
            ],
        }
        """
        return super().list(request, *args, **kwargs)

    def perform_update(self, serializer):
        serializer.save()

        if not serializer.validated_data.get("tags"):
            return

        instance: GsePackages = self.get_object()
        tags: QuerySet = gse_package_handler.get_tag_objs(instance.project, instance.version)
        tag_name__tag_obj_map: Dict[str, Tag] = {tag.name: tag for tag in tags}
        tag_names: List[str] = list(tag_name__tag_obj_map.keys())

        # 只根据name修改对应的description，不支持标签的新增
        for tag_dict in serializer.validated_data["tags"]:
            # 如果name不存在，无法修改对应的description
            if tag_dict["name"] not in tag_names:
                continue

            tag_obj: Tag = tag_name__tag_obj_map[tag_dict["name"]]
            tag_obj.description = tag_dict["description"]
            tag_obj.save(update_fields=["description"])

    @swagger_auto_schema(
        operation_summary="操作类动作：启用/停用",
        body_in=pkg_manage.OperateSerializer,
        responses={200: pkg_manage.PackageSerializer},
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    def update(self, request, validated_data, *args, **kwargs):
        """
        return: {
            "id": 1,
            "pkg_name": "pkg_name",
            "version": "1.1.1",
            "os": "Linux",
            "cpu_arch": "x86_64",
            "tags": [{"id": "stable", "name": "稳定版本"}],
            "creator": "string",
            "pkg_ctime": "2019-08-24 14:15:22",
            "is_ready": True,
        }
        """
        instance: GsePackages = self.get_object()
        serializer = pkg_manage.OperateSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        updated_instance: GsePackages = self.get_object()

        return Response(pkg_manage.PackageSerializer(updated_instance).data)

    @swagger_auto_schema(
        operation_summary="删除安装包",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    def destroy(self, request, *args, **kwargs):
        # todo: 前端需求，等联调完改回来
        super(PackageManageViewSet, self).destroy(request, *args, **kwargs)
        return Response(data=[])

    @swagger_auto_schema(
        operation_summary="获取快速筛选信息",
        manual_parameters=[
            openapi.Parameter(
                "project", in_=openapi.TYPE_STRING, description="区分gse_agent, gse_proxy", type=openapi.TYPE_STRING
            )
        ],
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    @action(detail=False, methods=["GET"])
    def quick_search_condition(self, request, *args, **kwargs):
        """
        return: [
            {"name": "操作系统/架构", "id": "os_cpu_arch", "children": [
                {"name": "Linux_x86_64", "id": "linux_x86_64", "count": 10},
                {"name": "Linux_x86", "id": "linux_x86", "count": 10},
                {"name": "ALL", "id": "ALL", "count": 20},
            ]},
            {"name": "版本号", "id": "version", "children": [
                {"name": "2.1.8", "id": "2.1.8", "count": 10},
                {"name": "2.1.7", "id": "2.1.7", "count": 10},
                {"name": "ALL", "id": "ALL", "count": 20},
            ]},
        ]
        """
        gse_packages = self.filter_queryset(self.get_queryset()).values("version", "os", "cpu_arch", "version_log")

        version__count_map: Dict[str, int] = defaultdict(int)
        os_cpu_arch__count_map: Dict[str, int] = defaultdict(int)
        version__version_log_map: Dict[str, str] = defaultdict(str)
        os_cpu_arch__version_log_map: Dict[str, str] = defaultdict(str)

        for package in gse_packages:
            version, os_cpu_arch = package["version"], f"{package['os']}_{package['cpu_arch']}"

            version__count_map[version] += 1
            os_cpu_arch__count_map[os_cpu_arch] += 1

            if version not in version__version_log_map:
                version__version_log_map[version] = package["version_log"]

            if os_cpu_arch not in os_cpu_arch__version_log_map:
                os_cpu_arch__version_log_map[os_cpu_arch] = package["version_log"]

        return Response(
            [
                {
                    "name": _("操作系统/架构"),
                    "id": "os_cpu_arch",
                    "children": [
                        {
                            "id": os_cpu_arch,
                            "name": os_cpu_arch.capitalize(),
                            "count": count,
                            "description": os_cpu_arch__version_log_map[os_cpu_arch],
                        }
                        for os_cpu_arch, count in os_cpu_arch__count_map.items()
                    ],
                    "count": sum(os_cpu_arch__count_map.values()),
                },
                {
                    "name": _("版本号"),
                    "id": "version",
                    "children": [
                        {
                            "id": version,
                            "name": version.capitalize(),
                            "count": count,
                            "description": version__version_log_map[version],
                        }
                        for version, count in version__count_map.items()
                    ],
                    "count": sum(version__count_map.values()),
                },
            ]
        )

    @swagger_auto_schema(
        operation_summary="Agent包上传",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        responses={HTTP_200_OK: pkg_manage.UploadResponseSerializer},
    )
    @action(detail=False, methods=["POST"], serializer_class=pkg_manage.UploadSerializer)
    def upload(self, request):
        """
        return: {
            "id": 116,
            "name": "HR7vt0c_gse_ce-v2.1.3-beta.13.tgz",
            "pkg_size": "336252435"
        }
        """
        request_serializer = self.serializer_class(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data

        if validated_data.get("overload"):
            storage = get_storage()
            package_file = validated_data["package_file"]

            upload_package = UploadPackage.objects.filter(
                file_name=package_file.name, creator=get_request_username(), module=TargetType.AGENT.value
            ).first()

            if upload_package and storage.exists(name=upload_package.file_path):
                storage.delete(name=upload_package.file_path)
                upload_package.delete()

        # todo: 如果不对upload进行侵入式修改，会存在大量的storage upload和download操作
        res = PackageTools.upload(package_file=validated_data["package_file"], module=TargetType.AGENT.value)

        if "result" in res:
            return JsonResponse(res)
        else:
            return Response(res)

    @swagger_auto_schema(
        operation_summary="解析Agent包",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        responses={HTTP_200_OK: pkg_manage.ParseResponseSerializer},
    )
    @action(detail=False, methods=["POST"], serializer_class=pkg_manage.ParseSerializer)
    def parse(self, request):
        """
        return: {
            "description": "test",
            "packages": [
                {
                    "pkg_abs_path": "xxx/xxxxx",
                    "pkg_name": "gseagent_2.1.7_linux_x86_64.tgz",
                    "module": "agent",
                    "version": "2.1.7",
                    "config_templates": [],
                    "os": "x86_64",
                },
                {
                    "pkg_abs_path": "xxx/xxxxx",
                    "pkg_name": "gseagent_2.1.7_linux_x86.tgz",
                    "module": "agent",
                    "version": "2.1.7",
                    "config_templates": [],
                    "os": "x86",
                },
            ],
        }
        """
        return Response(NodeApi.agent_parse(self.validated_data))

    @swagger_auto_schema(
        operation_summary="创建Agent包注册任务",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        responses={HTTP_200_OK: pkg_manage.AgentRegisterTaskSerializer},
    )
    @action(detail=False, methods=["POST"], serializer_class=pkg_manage.AgentRegisterSerializer)
    def create_register_task(self, request):
        """
        return: {"task_id": 1}
        """
        validated_data = self.validated_data

        response = NodeApi.sync_task_create(
            {
                "task_name": SyncTaskType.REGISTER_GSE_PACKAGE.value,
                "task_params": {
                    "file_name": validated_data["file_name"],
                    "tags": validated_data["tags"],
                },
            }
        )

        return Response({"task_id": response["task_id"]})

    @swagger_auto_schema(
        operation_summary="查询Agent包注册任务",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        query_in=pkg_manage.AgentRegisterTaskSerializer,
        responses={HTTP_200_OK: pkg_manage.AgentRegisterTaskResponseSerializer},
    )
    @action(detail=False, methods=["GET"], serializer_class=pkg_manage.AgentRegisterTaskSerializer)
    def query_register_task(self, request, validated_data):
        """
        return: {
            "is_finish": True,
            "status": "SUCCESS",
            "message": "",
        }
        """
        validated_data = self.validated_data

        return Response(NodeApi.sync_task_status({"task_id": validated_data["task_id"]}))

    @swagger_auto_schema(
        operation_summary="获取Agent包标签",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        responses={HTTP_200_OK: pkg_manage.TagSerializer(many=True)},
    )
    @action(detail=False, methods=["GET"], serializer_class=pkg_manage.TagProjectSerializer)
    def tags(self, request):
        """
        return: [
            {
                "name": "builtin",
                "description": "内置标签",
                "children": [
                    {
                        "name": "stable",
                        "description": "稳定版本"
                    }
                ]
            },
            {
                "name": "custom",
                "description": "自定义标签",
                "children": [
                    {
                        "name": "tag4",
                        "description": "标签4"
                    },
                    {
                        "name": "tag5",
                        "description": "标签5"
                    }
                ]
            }
        ]
        """
        validated_data = self.validated_data
        try:
            return Response(
                gse_package_handler.handle_tags(
                    tags=Tag.objects.filter(
                        target_id=GsePackageDesc.objects.get(project=validated_data["project"]).id,
                        created_by=get_request_username(),
                    ).values("id", "name", "description"),
                    tag_description=request.query_params.get("tag_description"),
                    unique=True,
                    get_template_tags=False,
                )
            )
        except GsePackageDesc.DoesNotExist:
            raise exceptions.ModelInstanceNotFoundError(model_name="GsePackageDesc")

    @swagger_auto_schema(
        operation_summary="获取Agent包版本",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
        responses={HTTP_200_OK: pkg_manage.PackageDescResponseSerializer},
    )
    @action(detail=False, methods=["GET"])
    def version(self, request):
        """
        return: {
            "total": 10,
            "list": [
                {
                    "version": "2.1.2",
                    "tags": [{"id": "stable", "name": "稳定版本"}],
                    "is_ready": True,
                    "description": "",
                    "packages": [
                        {
                            "pkg_name": "gseagent-2.1.2.tgz",
                            "tags": [{"id": "stable", "name": "稳定版本1"}, {"id": "latest", "name": "最新版本"}],
                        },
                        {
                            "pkg_name": "gseagent-2.1.2.tgz",
                            "tags": [{"id": "stable", "name": "稳定版本2"}, {"id": "latest", "name": "最新版本"}],
                        }
                    ],
                }
            ],
        }
        """
        gse_packages = self.filter_queryset(self.get_queryset()).values("version", "project", "pkg_name")

        version__pkg_version_map: Dict[str, Dict[str, Any]] = {}
        for package in gse_packages:
            version, project, pkg_name = package["version"], package["project"], package.pop("pkg_name")
            tags: List[Dict[str, Any]] = gse_package_handler.get_tags(version=version, project=project, to_top=True)

            if version not in version__pkg_version_map:
                # 初始化某个版本的包
                version__pkg_version_map[version] = {
                    "version": version,
                    "project": project,
                    "packages": [],
                    "tags": tags,
                }

            # 添加小包包名和小包标签信息
            version__pkg_version_map[version]["packages"].append({"pkg_name": pkg_name, "tags": tags})

            # 聚合小包之间的共同标签
            common_tags = version__pkg_version_map[version]["tags"]
            if common_tags != tags:
                version__pkg_version_map[version]["tags"] = [tag for tag in common_tags if tag in tags]

        return Response(list(version__pkg_version_map.values()))

    @swagger_auto_schema(
        operation_summary="获取已部署主机数量",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    @action(detail=False, methods=["POST"], serializer_class=pkg_manage.DeployedAgentCountSerializer)
    def deployed_hosts_count(self, request):
        validated_data = self.validated_data

        items = validated_data["items"]
        project = validated_data["project"]
        if not items:
            return Response()

        # 划分维度到主机和进程
        dimensions: List[str] = list(items[0].keys())
        host_dimensions: List[str] = [d for d in dimensions if d in [field.name for field in models.Host._meta.fields]]
        process_dimensions: List[str] = list(set(dimensions) - set(host_dimensions))

        # 主机筛选条件
        host_kwargs: Dict[str, list] = {
            f"{dimension}__in": [item[dimension] for item in items] for dimension in host_dimensions
        }

        # 进程筛选条件
        process_params: Dict[str, list] = {
            "conditions": [{"key": "status", "value": [constants.ProcStateType.RUNNING]}]
        }

        for dimension in process_dimensions:
            process_params["conditions"].append({"key": dimension, "value": [item[dimension] for item in items]})

        # 主机和进程连表查询
        host_queryset: QuerySet = HostQuerySqlHelper.multiple_cond_sql(
            params=process_params,
            biz_scope=[],
            need_biz_scope=False,
            is_proxy=False if project == constants.GsePackageCode.AGENT.value else True,
        ).filter(**host_kwargs)

        # 分组统计数量，使用values + annotate分组统计不管用，原因不详
        dimension__count_map = defaultdict(int)
        for host in host_queryset.values(*dimensions):
            dimension__count_map["|".join(host.get(d, "").lower() for d in dimensions)] += 1

        # 填充count到item
        for item in items:
            item["count"] = dimension__count_map.get("|".join(item.get(d, "").lower() for d in dimensions), 0)

        return Response(items)

    @swagger_auto_schema(
        operation_summary="创建agent标签",
        tags=PACKAGE_MANAGE_VIEW_TAGS,
    )
    @action(detail=False, methods=["POST"], serializer_class=pkg_manage.TagCreateSerializer)
    def create_agent_tags(self, request):
        # todo: 是否移植到apps.core.tag.views中？原有的tag标签创建只能创建单标签，
        #  且有些不太符合预期，在不考虑对原有标签进行修改的情况下，增加一个批量创建标签的接口，
        #  修改和删除沿用apps.core.tags.views中的接口
        validated_data = self.validated_data

        success_created_tag: List[Dict[str, str]] = []
        for tag in validated_data["tags"]:
            try:
                target_id = GsePackageDesc.objects.get(
                    project=validated_data["project"], category=CategoryType.official
                ).id
            except GsePackageDesc.DoesNotExist:
                target_id = GsePackageDesc.objects.create(
                    project=validated_data["project"], category=CategoryType.official
                ).id

            tag, _ = Tag.objects.get_or_create(
                defaults={"name": tag["name"]},
                description=tag["description"],
                target_id=target_id,
                target_type=TargetType.AGENT.value,
            )
            success_created_tag.append({"name": tag.name, "description": tag.description})

        return Response(data=success_created_tag)


# class AgentPackageDescViewSet(ModelViewSet):
#     queryset = models.AgentPackageDesc.objects.all()
#     # model = models.Packages
#     # http_method_names = ["get", "post"]
#     # ordering_fields = ("module",)
#     # serializer_class = pkg_manage.PackageSerializer
#     # filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)

#     # filter_fields = ("module", "creator", "is_ready", "version")

#     @swagger_auto_schema(
#         query_in=pkg_manage.PackageDescSearchSerializer,
# responses={200: pkg_manage.PackageDescResponseSerialiaer},
#         operation_summary="Agent版本列表",
#         tags=PACKAGE_DES_VIEW_TAGS,
#     )
#     def list(self, request, *args, **kwargs):

#         mock_data = {
#             "total": 10,
#             "list": [
#                 {
#                     "id": 1,
#                     "version": "2.1.2",
#                     "tags": [{"id": "stable", "name": "稳定版本"}],
#                     "is_ready": True,
#                     "description": "我是描述",
#                     "packages": [
#                         {
#                             "pkg_name": "gseagent-2.1.2.tgz",
#                             "tags": [{"id": "stable", "name": "稳定版本"}, {"id": "latest", "name": "最新版本"}],
#                         }
#                     ],
#                 }
#             ],
#         }
#         return Response(mock_data)
#         # return super().list(request, *args, **kwargs)
