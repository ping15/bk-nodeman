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
import json
import os
import random
from collections import ChainMap, defaultdict
from typing import Any, Dict, List, Union

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db.models import Count
from django.utils.translation import ugettext_lazy as _

from apps.component.esbclient import client_v2
from apps.core.files import core_files_constants
from apps.core.files.storage import get_storage
from apps.node_man import constants, exceptions, models, tools
from apps.node_man.constants import IamActionType
from apps.node_man.handlers.cmdb import CmdbHandler
from apps.node_man.handlers.iam import IamHandler
from apps.utils.basic import distinct_dict_list, list_slice
from apps.utils.batch_request import batch_request
from apps.utils.concurrent import batch_call
from apps.utils.files import md5sum
from apps.utils.local import get_request_username
from common.api import NodeApi


class PluginV2Handler:
    @staticmethod
    def upload(package_file: InMemoryUploadedFile, module: str) -> Dict[str, Any]:
        """
        将文件上传至
        :param package_file: InMemoryUploadedFile
        :param module: 所属模块
        :return:
        {
            "result": True,
            "message": "",
            "code": "00",
            "data": {
                "id": record.id,  # 上传文件记录ID
                "name": record.file_name,  # 包名
                "pkg_size": record.file_size,  # 大小，
            }
        }
        """
        with package_file.open("rb") as tf:

            # 计算上传文件的md5
            md5 = md5sum(file_obj=tf, closed=False)

            base_params = {"module": module, "md5": md5}

            # 如果采用对象存储，文件直接上传至仓库，并将返回的目标路径传到后台，由后台进行校验并创建上传记录
            # TODO 后续应该由前端上传文件并提供md5
            if settings.STORAGE_TYPE in core_files_constants.StorageType.list_cos_member_values():
                storage = get_storage()

                try:
                    storage_path = storage.save(name=os.path.join(settings.UPLOAD_PATH, tf.name), content=tf)
                except Exception as e:
                    raise exceptions.PluginUploadError(plugin_name=tf.name, error=e)

                return NodeApi.upload(
                    {
                        **base_params,
                        # 最初文件上传的名称，后台会使用该文件名保存并覆盖同名文件
                        "file_name": tf.name,
                        "file_path": storage_path,
                        "download_url": storage.url(storage_path),
                    }
                )

            else:

                response = requests.post(
                    url=settings.DEFAULT_FILE_UPLOAD_API,
                    data={
                        **base_params,
                        "bk_app_code": settings.APP_CODE,
                        "bk_username": get_request_username(),
                    },
                    # 本地文件系统仍通过上传文件到Nginx并回调后台
                    files={"package_file": tf},
                )

                return json.loads(response.content)

    @staticmethod
    def list_plugin(query_params: Dict):
        plugin_page = NodeApi.plugin_list(query_params)

        if query_params.get("simple_all"):
            return plugin_page

        operate_perms = []

        is_superuser = IamHandler().is_superuser(get_request_username())

        if not is_superuser and settings.USE_IAM:
            perms = IamHandler().fetch_policy(get_request_username(), {IamActionType.plugin_pkg_operate})
            operate_perms = perms[IamActionType.plugin_pkg_operate]

        for plugin in plugin_page["list"]:
            plugin.update(
                {
                    "category": constants.CATEGORY_DICT.get(plugin["category"], plugin["category"]),
                    "deploy_type": constants.DEPLOY_TYPE_DICT.get(plugin["deploy_type"], plugin["deploy_type"]),
                }
            )
            plugin["permissions"] = {
                "operate": plugin["id"] in operate_perms if not is_superuser and settings.USE_IAM else True
            }
        return plugin_page

    @staticmethod
    def fetch_config_variables(config_tpl_ids):
        config_tpls = list(
            models.PluginConfigTemplate.objects.filter(id__in=config_tpl_ids, is_release_version=True).values(
                "id", "name", "version", "is_main", "creator", "content"
            )
        )
        diff = set(config_tpl_ids) - {tpl["id"] for tpl in config_tpls}
        if diff:
            raise exceptions.PluginConfigTplNotExistError(_("插件配置模板{ids}不存在或已下线").format(ids=diff))

        for config_tpl in config_tpls:
            # 必然不成立条件，用于暂时关闭配置选择入口
            if "is_main" not in config_tpl:
                shield_content = tools.PluginV2Tools.shield_tpl_unparse_content(config_tpl["content"])
                config_tpl["variables"] = tools.PluginV2Tools.simplify_var_json(
                    tools.PluginV2Tools.parse_tpl2var_json(shield_content)
                )
            config_tpl.pop("content")
        return config_tpls

    @staticmethod
    def history(query_params: Dict):
        packages = NodeApi.plugin_history(query_params)
        if not packages:
            return packages
        nodes_counter = tools.PluginV2Tools.get_packages_node_numbers(
            [packages[0]["project"]], ["os", "cpu_arch", "version"]
        )
        for package in packages:
            package["nodes_number"] = nodes_counter.get(
                f"{package['os']}_{package['cpu_arch']}_{package['version']}", 0
            )
        return packages

    @staticmethod
    def operate(job_type: str, plugin_name: str, scope: Dict, steps: List[Dict]):
        bk_biz_scope = list(set([node["bk_biz_id"] for node in scope["nodes"]]))

        CmdbHandler().check_biz_permission(bk_biz_scope, IamActionType.plugin_operate)

        base_create_kwargs = {
            "is_main": True,
            "run_immediately": True,
            "plugin_name": plugin_name,
            # 非策略订阅在SaaS侧定义为一次性下发操作
            "category": models.Subscription.CategoryType.ONCE,
            "scope": scope,
            "bk_biz_scope": bk_biz_scope,
        }

        if job_type == constants.JobType.MAIN_INSTALL_PLUGIN:
            create_data = {**base_create_kwargs, "steps": steps}
            tools.PolicyTools.parse_steps(create_data, settings_key="config", simple_key="configs")
            tools.PolicyTools.parse_steps(create_data, settings_key="params", simple_key="params")

            create_data["steps"][0]["config"]["job_type"] = job_type
        else:
            config_templates = models.PluginConfigTemplate.objects.filter(plugin_name=plugin_name, is_main=True)
            create_data = {
                **base_create_kwargs,
                "steps": [
                    {
                        "config": {
                            "config_templates": distinct_dict_list(
                                [
                                    {
                                        "name": conf_tmpl.name,
                                        "version": "latest",
                                        "is_main": True,
                                        "os": conf_tmpl.os,
                                        "cpu_arch": conf_tmpl.cpu_arch,
                                    }
                                    for conf_tmpl in config_templates
                                ]
                            ),
                            "plugin_name": plugin_name,
                            "plugin_version": "latest",
                            "job_type": job_type,
                        },
                        "type": "PLUGIN",
                        "id": plugin_name,
                        "params": {},
                    }
                ],
            }

        create_result = NodeApi.create_subscription(create_data)
        create_result.update(
            tools.JobTools.create_job(
                job_type=job_type,
                subscription_id=create_result["subscription_id"],
                task_id=create_result["task_id"],
                bk_biz_scope=create_data["bk_biz_scope"],
            )
        )
        return create_result

    @staticmethod
    def fetch_package_deploy_info(projects: List[str], keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        获取插件包部署信息
        :param projects: 插件包
        :param keys: 聚合关键字
        :return: 聚合关键字 - 插件包部署信息
        """

        cache_deploy_number_template = "plugin_v2:fetch_package_deploy_info:{project}:{keys_combine_str}"

        # 以project & keys 粒度查询已缓存的数据
        cache_key__project_key_str__deploy_number_map = cache.get_many(
            cache_deploy_number_template.format(project=project, keys_combine_str="|".join(keys))
            for project in projects
        )

        projects__hit = set()
        key_str__deploy_number_cache_map = {}
        # 将缓存结构转化为查询函数的返回格式，并记录projects的命中情况
        for cache_key, project_key_str__deploy_number_map in cache_key__project_key_str__deploy_number_map.items():
            project_hit = cache_key.split(":", 3)[2]
            projects__hit.add(project_hit)

            key_str__deploy_number_cache_map.update(project_key_str__deploy_number_map)

        params_list = []
        projects__not_hit = list(set(projects) - projects__hit)
        if len(projects__not_hit) <= settings.CONCURRENT_NUMBER:
            for project in projects__not_hit:
                params_list.append({"projects": [project], "keys": keys})
        else:
            # 指定的projects太多，此时切片并发执行
            slice_projects_list = list_slice(projects__not_hit, limit=5)
            for slice_projects in slice_projects_list:
                params_list.append({"projects": slice_projects, "keys": keys})

        project_key_str__deploy_number_map_list: List[Dict[str, int]] = batch_call(
            func=tools.PluginV2Tools.get_packages_node_numbers, params_list=params_list, get_data=lambda x: x
        )
        key_str__deploy_number_uncache_map = dict(ChainMap(*project_key_str__deploy_number_map_list))

        # 按project维度重组查询所得的各个插件包部署数量
        project__key_str__deploy_number_map = defaultdict(dict)
        for key_str, deploy_number in key_str__deploy_number_uncache_map.items():
            project = key_str.split("_", 1)[0]
            project__key_str__deploy_number_map[project].update({key_str: deploy_number})

        # 缓存此前未命中的数据，仅保存查询时间较长的
        for project, project_key_str__deploy_number_map in project__key_str__deploy_number_map.items():
            # 过期时间分散设置，防止缓存雪崩
            cache.set(
                cache_deploy_number_template.format(project=project, keys_combine_str="|".join(keys)),
                project_key_str__deploy_number_map,
                constants.TimeUnit.DAY + random.randint(constants.TimeUnit.DAY, 2 * constants.TimeUnit.DAY),
            )

        key_str__deploy_info_map = {}
        all_key_str__deploy_number_map = dict(
            ChainMap(key_str__deploy_number_uncache_map, key_str__deploy_number_cache_map)
        )
        # 组装返回结构，此处虽然仅需部署数量信息，保留字典结构以便后续接口返回信息拓展
        for key_str, deploy_number in all_key_str__deploy_number_map.items():
            key_str__deploy_info_map[key_str] = {"nodes_number": deploy_number}
        return key_str__deploy_info_map

    @classmethod
    def fetch_resource_policy(cls, bk_biz_id: int, bk_obj_id: str, bk_inst_id: int) -> Dict:
        """
        查询资源策略
        """
        head_plugins: List[str] = tools.PluginV2Tools.fetch_head_plugins()
        # 部分插件仅面向 proxy，放入 head_plugins 会导致冗余的插件进程状态同步
        # 此处临时添加，继续通过 https://github.com/TencentBlueKing/bk-nodeman/issues/878 优化
        extra_plugins: List[str] = list(
            models.GsePluginDesc.objects.filter(name__in=["bkmonitorproxy"]).values_list("name", flat=True)
        )
        head_plugins = list(set(head_plugins + extra_plugins))
        resource_policy_qs = models.PluginResourcePolicy.objects.filter(
            bk_biz_id=bk_biz_id, bk_obj_id=bk_obj_id, bk_inst_id=bk_inst_id, plugin_name__in=head_plugins
        )
        # 暂时只支持服务模板，需扩展时可通过bk_obj_id进一步区分
        if bk_obj_id == constants.CmdbObjectId.SERVICE_TEMPLATE:
            hosts = batch_request(
                client_v2.cc.find_host_by_service_template,
                {"bk_service_template_ids": [bk_inst_id], "bk_biz_id": bk_biz_id, "fields": ["bk_host_id"]},
            )
        else:
            hosts = []
        bk_host_ids = [host["bk_host_id"] for host in hosts]
        is_default = not resource_policy_qs.exists()
        resource_policy = {
            plugin_name: {
                "plugin_name": plugin_name,
                "cpu": constants.PLUGIN_DEFAULT_CPU_LIMIT,
                "mem": constants.PLUGIN_DEFAULT_MEM_LIMIT,
                "statistics": {"total_count": 0, "running_count": 0, "terminated_count": 0},
            }
            for plugin_name in head_plugins
        }

        for policy in resource_policy_qs:
            resource_policy[policy.plugin_name] = {
                "plugin_name": policy.plugin_name,
                "cpu": policy.cpu,
                "mem": policy.mem,
                "statistics": {"total_count": 0, "running_count": 0, "terminated_count": 0},
            }

        statistics = (
            models.ProcessStatus.objects.filter(bk_host_id__in=bk_host_ids, name__in=head_plugins, is_latest=True)
            .values("name", "status")
            .annotate(count=Count("status"))
        )
        for stati in statistics:
            count = stati["count"]
            if stati["status"] == constants.ProcStateType.TERMINATED:
                resource_policy[stati["name"]]["statistics"]["terminated_count"] = count
            elif stati["status"] == constants.ProcStateType.RUNNING:
                resource_policy[stati["name"]]["statistics"]["running_count"] = count
            resource_policy[stati["name"]]["statistics"]["total_count"] += count
        return {"is_default": is_default, "resource_policy": list(resource_policy.values())}

    @classmethod
    def set_resource_policy(
        cls, bk_biz_id: int, bk_obj_id: str, bk_inst_id: int, resource_policy: List[Dict]
    ) -> Dict[str, List[int]]:
        """
        设置资源策略
        """
        # 查询数据库中已配置的资源策略
        plugin_name_policy_map = {
            policy.plugin_name: policy
            for policy in models.PluginResourcePolicy.objects.filter(bk_obj_id=bk_obj_id, bk_inst_id=bk_inst_id)
        }

        # 对比差异，得出需要重新设定的插件及主机
        diff_policy = []
        for policy in resource_policy:
            current_policy = plugin_name_policy_map.get(policy["plugin_name"])
            current_cpu = getattr(current_policy, "cpu", constants.PLUGIN_DEFAULT_CPU_LIMIT)
            current_mem = getattr(current_policy, "mem", constants.PLUGIN_DEFAULT_MEM_LIMIT)
            if current_cpu != policy["cpu"] or current_mem != policy["mem"]:
                diff_policy.append(policy)

        if not diff_policy:
            raise exceptions.PluginResourcePolicyNoDiff()
        # 重载插件应用最新的资源策略
        job_ids = []
        for policy in diff_policy:
            models.PluginResourcePolicy.objects.update_or_create(
                plugin_name=policy["plugin_name"],
                bk_biz_id=bk_biz_id,
                bk_obj_id=bk_obj_id,
                bk_inst_id=bk_inst_id,
                defaults=dict(cpu=policy["cpu"], mem=policy["mem"]),
            )
            job_id = cls.operate(
                job_type=constants.JobType.MAIN_RELOAD_PLUGIN,
                plugin_name=policy["plugin_name"],
                scope={
                    "node_type": models.Subscription.NodeType.SERVICE_TEMPLATE,
                    "object_type": models.Subscription.ObjectType.HOST,
                    "nodes": [
                        {
                            "bk_biz_id": bk_biz_id,
                            "bk_obj_id": models.Subscription.NodeType.SERVICE_TEMPLATE,
                            "bk_inst_id": bk_inst_id,
                        }
                    ],
                },
                steps=[],
            )["job_id"]
            job_ids.append(job_id)
        return {"job_id_list": job_ids}

    @classmethod
    def fetch_resource_policy_status(cls, bk_biz_id: int, bk_obj_id: str) -> List[Dict[str, Union[int, bool]]]:
        """查询资源策略状态"""
        exist_policy_bk_inst_ids = list(
            models.PluginResourcePolicy.objects.filter(bk_biz_id=bk_biz_id, bk_obj_id=bk_obj_id).values_list(
                "bk_inst_id", flat=True
            )
        )
        biz_inst_ids = []
        if bk_obj_id == constants.CmdbObjectId.SERVICE_TEMPLATE:
            biz_inst_ids = [
                service_template["id"] for service_template in CmdbHandler.get_biz_service_template(bk_biz_id=bk_biz_id)
            ]
        return [
            {"bk_inst_id": biz_inst_id, "is_default": biz_inst_id not in exist_policy_bk_inst_ids}
            for biz_inst_id in biz_inst_ids
        ]
