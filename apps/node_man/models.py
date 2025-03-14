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
import copy
import hashlib
import json
import operator
import os
import random
import shutil
import subprocess
import tarfile
import traceback
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from distutils.dir_util import copy_tree
from enum import Enum
from functools import cmp_to_key, reduce
from typing import Any, Dict, List, Optional, Set, Union

import requests
import six
from bkcrypto.contrib.django.fields import SymmetricTextField
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.encoding import force_text
from django.utils.functional import Promise
from django.utils.translation import get_language
from django.utils.translation import ugettext_lazy as _
from django_mysql.models import JSONField
from jinja2 import Template

from apps.backend.subscription.errors import PipelineExecuteFailed, SubscriptionNotExist
from apps.backend.subscription.render_functions import get_hosts_by_node
from apps.backend.utils.data_renderer import nested_render_data
from apps.core.files.storage import get_storage
from apps.exceptions import ValidationError
from apps.node_man import constants
from apps.node_man.exceptions import (
    AliveProxyNotExistsError,
    ApIDNotExistsError,
    CreateRecordError,
    HostNotExists,
    InstallChannelNotExistsError,
    UrlNotReachableError,
)
from apps.node_man.utils.endpoint import EndpointInfo
from apps.prometheus.models import (
    export_job_prometheus_mixin,
    export_subscription_prometheus_mixin,
)
from apps.utils import basic, files, orm, translation
from common.log import logger
from env.constants import GseVersion
from pipeline.parser import PipelineParser
from pipeline.service import task_service


class LazyJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Promise):
            return force_text(obj)
        return super(LazyJSONEncoder, self).default(obj)


class LazyJSONField(JSONField):
    _default_json_encoder = LazyJSONEncoder(allow_nan=False)


class GlobalSettings(models.Model):
    """
    配置表
    """

    class KeyEnum(Enum):
        """枚举全局配置KEY，避免散落在各处难以维护"""

        # 并发控制相关配置
        CONCURRENT_CONTROLLER_SETTINGS = "CONCURRENT_CONTROLLER_SETTINGS"
        # 订阅任务单Pipeline执行主机数
        TASK_HOST_LIMIT = "TASK_HOST_LIMIT"
        # DB 每批操作数
        BATCH_SIZE = "BATCH_SIZE"
        USE_TJJ = "USE_TJJ"  # 是否启用TJJ
        REGISTER_WIN_SERVICE_WITH_PASS = "REGISTER_WIN_SERVICE_WITH_PASS"  # 是否使用密码注册windows服务
        CONFIG_POLICY_BY_SOPS = "CONFIG_POLICY_BY_SOPS"  # 是否使用标准运维自动开通网络策略
        CONFIG_POLICY_BY_TENCENT_VPC = "CONFIG_POLICY_BY_TENCENT_VPC"  # 是否使用腾讯云SDK自动开通网络策略
        SECURITY_GROUP_TYPE = "SECURITY_GROUP_TYPE"  # 开通Proxy策略类型，可在BaseSecurityGroupAdapter中扩展
        APIGW_PUBLIC_KEY = "APIGW_PUBLIC_KEY"  # APIGW公钥，从PaaS接口获取或直接配到settings中
        SYNC_CMDB_HOST_BIZ_BLACKLIST = "SYNC_CMDB_HOST_BIZ_BLACKLIST"  # 排除掉黑名单业务的主机同步，比如 SA 业务，包含大量主机但无需同步
        LAST_SUB_TASK_ID = "LAST_SUB_TASK_ID"  # 定时任务 collect_auto_trigger_job 用于记录最后一个同步的 sub_task ID
        NOT_READY_TASK_INFO_MAP = "NOT_READY_TASK_INFO_MAP"  # 定时任务 collect_auto_trigger_job 记录未就绪 sub_task 信息
        HEAD_PLUGINS = "HEAD_PLUGINS"  # 插件类型名字
        INSTALL_DEFAULT_VALUES = "INSTALL_DEFAULT_VALUES"  # 安装默认值
        # 主机资源事件监听控制器 - HASH
        # limit - 处理事件步长，默认为 10 秒
        # seconds_to_wait_for_no_events - 无事件守护进程等待秒数
        APPLY_RESOURCE_WATCHED_EVENTS_CONTROLLER_KEY = "APPLY_RESOURCE_WATCHED_EVENTS_CONTROLLER_KEY"
        # 【临时变量】Agent 2.0 安装版本，过渡到 Agent 包管理后废除
        GSE_AGENT2_VERSION = "GSE_AGENT2_VERSION"
        # 【临时变量】主机安装动作新增推送身份动作, cc版本稳定后废除
        ENABLE_PUSH_HOST_IDENTIFIER = "ENABLE_PUSH_HOST_IDENTIFIER"
        # 插件配置公共常量支持 issue: https://github.com/TencentBlueKing/bk-nodeman/issues/1500
        PLUGIN_COMMON_CONSTANTS = "PLUGIN_COMMON_CONSTANTS"
        # GSE 2.0 灰度列表
        GSE2_GRAY_SCOPE_LIST = "GSE2_GRAY_SCOPE_LIST"
        # GSE 2.0 Linux Agent/Proxy 进程托管（拉起）方式，默认为 rclocal
        GSE2_LINUX_AUTO_TYPE = "GSE2_LINUX_AUTO_TYPE"
        # 禁用订阅业务列表
        DISABLE_SUBSCRIPTION_SCOPE_LIST = "DISABLE_SUBSCRIPTION_SCOPE_LIST"
        GSE2_GRAY_AP_MAP = "GSE2_GRAY_AP_MAP"
        # 是否同步 cc 的CPU架构
        SYNC_CMDB_HOST_APPLY_CPU_ARCH = "SYNC_CMDB_HOST_APPLY_CPU_ARCH"
        # 同步 cc 主机操作系统时，是否 bk_os_type 匹配优先与 bk_os_name
        SYNC_CMDB_HOST_OS_TYPE_PRIORITY = "SYNC_CMDB_HOST_OS_TYPE_PRIORITY"
        # 是否安装额外AGENT
        IS_INSTALL_OTHER_AGENT_V1 = "IS_INSTALL_OTHER_AGENT_V1"
        IS_INSTALL_OTHER_AGENT_V2 = "IS_INSTALL_OTHER_AGENT_V2"
        INSTALL_OTHER_AGENT_V1_BIZ_BLACKLIST = "INSTALL_OTHER_AGENT_V1_BIZ_BLACKLIST"
        INSTALL_OTHER_AGENT_V2_BIZ_BLACKLIST = "INSTALL_OTHER_AGENT_V2_BIZ_BLACKLIST"
        NEED_TO_WAIT_EXTRA_INSTALL_COMPLETE = "NEED_TO_WAIT_EXTRA_INSTALL_COMPLETE"
        # P-Agent 安装脚本名称
        SETUP_PAGENT_SCRIPT_FILENAME = "SETUP_PAGENT_SCRIPT_FILENAME"
        # 周期删除订阅任务数据
        CLEAN_SUBSCRIPTION_DATA_MAP = "CLEAN_SUBSCRIPTION_DATA_MAP"
        # 是否开启Agent包管理
        ENABLE_AGENT_PKG_MANAGE = "ENABLE_AGENT_PKG_MANAGE"
        # 云梯策略相关配置
        YUNTI_POLICY_CONFIGS = "YUNTI_POLICY_CONFIGS"

    key = models.CharField(_("键"), max_length=255, db_index=True, primary_key=True)
    v_json = JSONField(_("值"))

    def map_values(self, objs, source, target):
        """
        列表内字典转一键多值字典
        """

        ret = {}
        for obj in objs:
            ret[source(obj)] = target(obj)
        return ret

    def fetch_isp(self):
        isps = dict(GlobalSettings.objects.filter(key="isp").values_list("key", "v_json")).get("isp", [])
        result = self.map_values(
            isps, lambda isp: isp["isp"], lambda isp: {"isp_name": isp["isp_name"], "isp_icon": isp["isp_icon"]}
        )

        return result

    @classmethod
    def get_config(cls, key=None, default=None):
        try:
            return cls.objects.get(key=key).v_json
        except cls.DoesNotExist:
            return default

    @classmethod
    def set_config(cls, key, value):
        cls.objects.create(key=key, v_json=value)

    @classmethod
    def update_config(cls, key, value):
        cls.objects.filter(key=key).update(v_json=value)

    class Meta:
        verbose_name = _("配置表（GlobalSettings）")
        verbose_name_plural = _("配置表（GlobalSettings）")


class IdentityData(models.Model):
    bk_host_id = models.IntegerField(_("主机ID"), primary_key=True)
    auth_type = models.CharField(
        _("认证类型"), max_length=45, choices=constants.AUTH_CHOICES, default=constants.AuthType.PASSWORD
    )
    account = models.CharField(_("账户名"), max_length=45, default="")
    password = SymmetricTextField(_("密码"), blank=True, null=True)
    port = models.IntegerField(_("端口"), null=True, default=22)
    key = SymmetricTextField(_("密钥"), blank=True, null=True)
    extra_data = JSONField(_("额外认证资料"), blank=True, null=True)
    retention = models.IntegerField(_("保留天数"), default=1)
    updated_at = models.DateTimeField(_("更新时间"), null=True, auto_now=False)

    class Meta:
        verbose_name = _("临时认证数据（IdentityData）")
        verbose_name_plural = _("临时认证数据（IdentityData）")

    @classmethod
    def host_id_identity_data_map(cls, **conditions):
        if not conditions:
            return {}
        return {host.bk_host_id: host for host in cls.objects.filter(**conditions)}


class Host(models.Model):
    bk_host_id = models.IntegerField(_("CMDB主机ID"), primary_key=True)
    bk_agent_id = models.CharField(_("AgentID"), max_length=64, db_index=True, blank=True, null=True)
    bk_biz_id = models.IntegerField(_("业务ID"), db_index=True)
    bk_cloud_id = models.IntegerField(_("管控区域ID"), db_index=True)
    bk_host_name = models.CharField(_("主机名称"), max_length=128, db_index=True, blank=True, null=True, default="")
    bk_addressing = models.CharField(_("寻址方式"), max_length=16, default=constants.CmdbAddressingType.STATIC.value)

    inner_ip = models.CharField(_("内网IP"), max_length=15, db_index=True)
    outer_ip = models.CharField(_("外网IP"), max_length=15, blank=True, null=True, default="")
    login_ip = models.CharField(_("登录IP"), max_length=45, blank=True, null=True, default="")
    data_ip = models.CharField(_("数据IP"), max_length=45, blank=True, null=True, default="")

    inner_ipv6 = models.CharField(_("内网IPv6"), max_length=45, blank=True, null=True, default="")
    outer_ipv6 = models.CharField(_("外网IPv6"), max_length=45, blank=True, null=True, default="")

    os_type = models.CharField(
        _("操作系统"), max_length=16, choices=constants.OS_CHOICES, default=constants.OsType.LINUX, db_index=True
    )
    cpu_arch = models.CharField(
        _("CPU架构"), max_length=16, choices=constants.CPU_CHOICES, default=constants.CpuType.x86_64, db_index=True
    )
    os_version = models.CharField(_("操作系统版本"), max_length=32, blank=True, null=True, db_index=True, default="")
    node_type = models.CharField(_("节点类型"), max_length=16, choices=constants.NODE_CHOICES, db_index=True)
    node_from = models.CharField(_("节点来源"), max_length=45, choices=constants.NODE_FROM_CHOICES, default="NODE_MAN")
    is_manual = models.BooleanField(_("是否手动安装"), default=False)

    install_channel_id = models.IntegerField(_("安装通道"), null=True)
    ap_id = models.IntegerField(_("接入点ID"), null=True, db_index=True)
    gse_v1_ap_id = models.IntegerField(_("GSE1.0接入点ID"), null=True, blank=True, db_index=True)
    upstream_nodes = JSONField(_("上游节点"), default=list)

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("更新时间"), null=True, auto_now=False, db_index=True)

    extra_data = JSONField(_("额外数据"), blank=True, null=True)

    @classmethod
    def get_by_host_info(cls, host_info):
        """
        根据 host_info 获取 Host 对象
        :param host_info: [
            {
                "ip": "127.0.0.1",
                "bk_cloud_id": 0,
                "bk_supplier_id": 0
            }
        ]
        or {
                "bk_host_id": 1
            }
        """

        bk_host_id: Optional[int] = host_info.get("bk_host_id")
        if bk_host_id:
            try:
                return Host.objects.get(bk_host_id=bk_host_id)
            except Host.DoesNotExist:
                exception = _("bk_host_id={bk_host_id} 主机信息不存在").format(bk_host_id=bk_host_id)
        else:
            bk_cloud_id: int = host_info["bk_cloud_id"]
            host_inner_ip: Optional[str] = host_info.get("bk_host_innerip")

            # 入参不做限制，优先使用参数 bk_host_innerip 作为查询条件
            if host_inner_ip:
                filter_q = Q(inner_ip=host_inner_ip, bk_cloud_id=bk_cloud_id)
                exception_ip: str = host_inner_ip
            else:
                ip: str = host_info["ip"]
                exception_ip: str = ip
                if basic.is_v6(ip):
                    filter_q = Q(inner_ipv6=basic.exploded_ip(ip), bk_cloud_id=bk_cloud_id)
                else:
                    filter_q = Q(inner_ip=ip, bk_cloud_id=bk_cloud_id)
            host: Host = Host.objects.filter(filter_q).first()
            if host:
                return host
            else:
                exception = _("{bk_cloud_id}:{ip} 主机信息不存在").format(ip=exception_ip, bk_cloud_id=bk_cloud_id)
        raise HostNotExists(exception)

    @classmethod
    def host_id_obj_map(cls, **conditions):
        if not conditions:
            return {}
        return {host.bk_host_id: host for host in cls.objects.filter(**conditions)}

    @classmethod
    def get_random_alive_proxy(cls, proxies):
        """
        随机选一台可用的proxy
        """
        alive_proxies = [proxy for proxy in proxies if proxy.status == constants.ProcStateType.RUNNING]
        if not alive_proxies:
            raise AliveProxyNotExistsError(_("主机所属「管控区域」不存在可用Proxy"))
        else:
            return random.choice(alive_proxies)

    @property
    def identity(self) -> IdentityData:
        if not getattr(self, "_identity", None):
            self._identity, created = IdentityData.objects.get_or_create(bk_host_id=self.bk_host_id)
        return self._identity

    @property
    def ap(self):
        if getattr(self, "_ap", None):
            return self._ap
        # 未选择接入点时，默认取第一个接入点
        if self.ap_id == constants.DEFAULT_AP_ID:
            self._ap = AccessPoint.get_default_ap()
        else:
            try:
                self._ap = AccessPoint.objects.get(pk=self.ap_id)
            except AccessPoint.DoesNotExist:
                raise ApIDNotExistsError({"ap_id": self.ap_id})
        return self._ap

    @property
    def install_channel(self):
        if getattr(self, "_install_channel", None):
            return self._install_channel

        jump_server = None
        upstream_servers = {}
        # 指定了安装通道，使用安装通道的信息作为跳板和上游
        if self.install_channel_id:
            try:
                install_channel = InstallChannel.objects.get(pk=self.install_channel_id)
            except InstallChannel.DoesNotExist:
                raise InstallChannelNotExistsError
            jump_server_ip = random.choice(install_channel.jump_servers)
            filter_kwargs = {
                "bk_cloud_id": self.bk_cloud_id,
                "bk_addressing": constants.CmdbAddressingType.STATIC.value,
            }
            if basic.is_v6(jump_server_ip):
                filter_kwargs["inner_ipv6"] = jump_server_ip
            else:
                filter_kwargs["inner_ip"] = jump_server_ip

            try:
                jump_server = Host.objects.get(**filter_kwargs)
            except Host.DoesNotExist:
                raise HostNotExists(_("安装节点主机{inner_ip}不存在，请确认是否已安装AGENT").format(inner_ip=jump_server_ip))
            upstream_servers = install_channel.upstream_servers
        # TODO 这两个分支并未使用到，非安装通道场景是通过 fetch_gse_servers_info 获取实际的上游
        # 管控区域未指定安装通道的，用proxy作为跳板和上游
        # elif self.bk_cloud_id and self.node_type != constants.NodeType.PROXY:
        #     proxy_ips = [proxy.inner_ip or proxy.inner_ipv6 for proxy in self.proxies]
        #     jump_server = self.get_random_alive_proxy()
        #     upstream_servers = {"taskserver": proxy_ips, "btfileserver": proxy_ips, "dataserver": proxy_ips}
        # # 普通直连的情况，无需跳板，使用接入点的数据
        # else:
        #     jump_server = None
        #     upstream_servers = {
        #         "taskserver": self.ap.cluster_endpoint_info.inner_hosts,
        #         "btfileserver": self.ap.file_endpoint_info.inner_hosts,
        #         "dataserver": self.ap.data_endpoint_info.inner_hosts,
        #     }
        self._install_channel = jump_server, upstream_servers
        return self._install_channel

    @property
    def agent_config(self):
        """不要在循环中使用该方法，会有 n+1 查询问题"""
        os_type = self.os_type.lower()
        # 非 Windows 机器共用 Linux 配置
        if self.os_type != constants.OsType.WINDOWS:
            os_type = constants.OsType.LINUX.lower()
        return self.ap.agent_config[os_type]

    @property
    def proxies(self):
        if getattr(self, "_proxies", None):
            return self._proxies

        proxy_objs = Host.objects.filter(bk_cloud_id=self.bk_cloud_id, node_type=constants.NodeType.PROXY)
        proxy_host_ids = [proxy.bk_host_id for proxy in proxy_objs]

        alive_proxies = ProcessStatus.objects.filter(
            bk_host_id__in=proxy_host_ids,
            name=ProcessStatus.GSE_AGENT_PROCESS_NAME,
            status=constants.ProcStateType.RUNNING,
        )
        host_id_status_map = {proxy.bk_host_id: proxy.status for proxy in alive_proxies}
        for proxy in proxy_objs:
            proxy.status = host_id_status_map.get(proxy.bk_host_id) or constants.ProcStateType.TERMINATED
        self._proxies = proxy_objs
        return self._proxies

    @proxies.setter
    def proxies(self, value):
        self._proxies = value

    @property
    def status(self):
        if getattr(self, "_status", None):
            return self._status
        return None

    @status.setter
    def status(self, value):
        self._status = value

    class Meta:
        verbose_name = _("主机（Host）")
        verbose_name_plural = _("主机（Host）")
        ordering = ["-updated_at", "-bk_host_id"]


class ProcessStatus(models.Model):
    class SourceType(object):
        DEFAULT = "default"
        SUBSCRIPTION = "subscription"
        DEBUG = "debug"

    SOURCE_TYPE_CHOICES = (
        (SourceType.DEFAULT, _("默认")),
        (SourceType.SUBSCRIPTION, _("订阅")),
        (SourceType.DEBUG, _("调试")),
    )
    GSE_AGENT_PROCESS_NAME = "gseagent"

    bk_host_id = models.IntegerField(_("主机ID"), db_index=True)
    name = models.CharField(_("进程名称"), max_length=45, default=GSE_AGENT_PROCESS_NAME, db_index=True)
    status = models.CharField(
        _("进程状态"),
        max_length=45,
        choices=constants.PROC_STATE_CHOICES,
        default=constants.ProcStateType.UNKNOWN,
        db_index=True,
    )
    is_auto = models.CharField(
        _("是否自动启动"), max_length=45, choices=constants.AUTO_STATE_CHOICES, default=constants.AutoStateType.AUTO
    )
    version = models.CharField(_("进程版本"), max_length=45, blank=True, null=True, default="", db_index=True)
    proc_type = models.CharField(
        _("进程类型"), db_index=True, max_length=45, choices=constants.PROC_CHOICES, default=constants.ProcType.AGENT
    )

    configs = JSONField(_("配置文件"), default=list)
    listen_ip = models.CharField(_("监听IP"), max_length=45, null=True)
    listen_port = models.IntegerField(_("监听端口"), null=True)

    setup_path = models.TextField(_("二进制文件所在路径"), default="")
    log_path = models.TextField(_("日志路径"), default="")
    data_path = models.TextField(_("数据文件路径"), default="")
    pid_path = models.TextField(_("pid文件路径"), default="")

    group_id = models.CharField(_("插件组ID"), max_length=50, default="", db_index=True)
    source_type = models.CharField(
        _("来源类型"), db_index=True, max_length=32, default=SourceType.DEFAULT, choices=SOURCE_TYPE_CHOICES
    )
    source_id = models.CharField(_("来源ID"), db_index=True, max_length=32, default=None, null=True)

    retry_times = models.IntegerField("重试次数", default=0)
    bk_obj_id = models.CharField(_("CMDB对象ID"), max_length=32, default=None, null=True)
    is_latest = models.BooleanField(_("是否是最新记录"), default=False)

    @classmethod
    def hosts_agent_status_map(cls, bk_host_ids: List[int]) -> Dict[int, str]:
        """主机AGENT状态映射"""
        agent_statuses = cls.objects.filter(bk_host_id__in=bk_host_ids, name=cls.GSE_AGENT_PROCESS_NAME).values(
            "bk_host_id", "status"
        )
        host_id_agent_status_map = {
            agent_status["bk_host_id"]: agent_status["status"] for agent_status in agent_statuses
        }
        return host_id_agent_status_map

    @classmethod
    def fetch_process_statuses_by_host_id(cls, bk_host_id: int, source_type: str = None) -> List:
        """
        :param bk_host_id: 主机ID
        :param source_type: 来源类型
        :return:
        """
        qs = cls.objects.filter(bk_host_id=bk_host_id)
        if source_type:
            qs = qs.filter(source_type=source_type)
        return list(qs.values("id", "source_type", "source_id", "name", "version", "status"))

    class Meta:
        index_together = [
            ["source_type", "proc_type"],
        ]
        unique_together = (("bk_host_id", "listen_port"),)
        verbose_name = _("进程状态（ProcessStatus）")
        verbose_name_plural = _("进程状态（ProcessStatus）")


class AccessPoint(models.Model):
    name = models.CharField(_("接入点名称"), max_length=255)
    ap_type = models.CharField(_("接入点类型"), max_length=255, default="user")
    region_id = models.CharField(_("区域id"), max_length=255, default="", blank=True, null=True)
    city_id = models.CharField(_("城市id"), max_length=255, default="", blank=True, null=True)
    gse_version = models.CharField(
        _("GSE 版本"), max_length=32, default=GseVersion.V1.value, choices=GseVersion.list_choices()
    )
    btfileserver = JSONField(_("GSE BT文件服务器列表"))
    dataserver = JSONField(_("GSE 数据服务器列表"))
    taskserver = JSONField(_("GSE 任务服务器列表"))
    zk_hosts = JSONField(_("ZK服务器列表"))
    zk_account = models.CharField(_("ZK账号"), max_length=255, default="", blank=True, null=True)
    zk_password = SymmetricTextField(_("密码"), blank=True, null=True)
    package_inner_url = models.TextField(_("安装包内网地址"))
    package_outer_url = models.TextField(_("安装包外网地址"))
    # 历史遗留命名，现在该路径表示存储源的存储目录路径
    nginx_path = models.TextField(_("Nginx路径"), blank=True, null=True)
    agent_config = JSONField(_("Agent配置信息"))
    status = models.CharField(_("接入点状态"), max_length=255, default="", blank=True, null=True)
    description = models.TextField(_("接入点描述"))
    is_enabled = models.BooleanField(_("是否启用"), default=True)
    is_default = models.BooleanField(_("是否默认接入点，不可删除"), default=False)
    creator = JSONField(_("接入点创建者"), default=("admin",))
    port_config = JSONField(_("GSE端口配置"), default=dict)
    proxy_package = JSONField(_("Proxy上的安装包"), default=list)
    outer_callback_url = models.CharField(_("节点管理外网回调地址"), max_length=128, blank=True, null=True, default="")
    callback_url = models.CharField(_("节点管理内网回调地址"), max_length=128, blank=True, null=True, default="")

    @property
    def file_endpoint_info(self) -> EndpointInfo:
        return EndpointInfo(inner_server_infos=self.btfileserver, outer_server_infos=self.btfileserver)

    @property
    def data_endpoint_info(self) -> EndpointInfo:
        return EndpointInfo(inner_server_infos=self.dataserver, outer_server_infos=self.dataserver)

    @property
    def cluster_endpoint_info(self) -> EndpointInfo:
        return EndpointInfo(inner_server_infos=self.taskserver, outer_server_infos=self.taskserver)

    @classmethod
    def ap_id_obj_map(cls):
        all_ap = cls.objects.all()
        ap_map: Dict[int, cls] = {ap.id: ap for ap in all_ap}
        # 未选择接入点的则取默认接入点
        ap_map.update({constants.DEFAULT_AP_ID: all_ap[0], None: all_ap[0]})
        return ap_map

    @classmethod
    def get_default_ap(cls):
        """
        获取默认接入点
        :return:
        """
        default_ap = cls.objects.first()
        if default_ap is None:
            raise ApIDNotExistsError(_("默认接入点不存在"))
        return default_ap

    def get_agent_config(self, os_type: str) -> Dict[str, Any]:
        """
        获取指定操作系统的 Agent 配置信息
        :param os_type: 操作系统类型，兼容 constants.OsType 的 大小写
        :return:
        """
        os_type = os_type.lower()
        if os_type in [
            constants.OsType.AIX.lower(),
            constants.OsType.SOLARIS.lower(),
        ]:
            os_type = constants.OsType.LINUX.lower()
        return self.agent_config[os_type]

    @staticmethod
    def check_callback_url_reachable(callback_url: str, raise_exception: bool = False) -> Dict[str, Union[str, bool]]:
        """
        校验回调地址是否可达
        :param callback_url: 回调地址
        :param raise_exception: 是否抛出异常
        :return:
        """

        def _raise_exc_or_return(_err_msg: str):
            if raise_exception:
                raise UrlNotReachableError(_err_msg)
            return {"result": False, "err_msg": _err_msg}

        try:
            response = requests.get(url=f"{callback_url}/version")
            version_info = json.loads(response.content)
        except Exception as exc:
            return _raise_exc_or_return(_("回调地址请求失败，报错信息 -> {exc}").format(exc=exc))
        if version_info.get("module") != "backend":
            return _raise_exc_or_return(_("回调地址指向站点非节点管理后台"))
        return {"result": True}

    @classmethod
    def test(cls, params: dict):
        """
        接入点可用性测试
        :param params: Dict
        {
            "servers": [
                {
                    "inner_ip": "127.0.0.1",
                    "outer_ip": "127.0.0.2"
                }
            ],
            "package_inner_url": "http://127.0.0.1/download/",
            "package_outer_url": "http://127.0.0.2/download/"
        }
        :return:
        """

        @translation.RespectsLanguage(language=get_language())
        def _check_ip(ip: str, _logs: list):
            cmd_args: List[str] = ["ping", "-c", "1", ip, "-i", "1"]
            if basic.is_v6(ip):
                cmd_args.append("-6")
            try:
                subprocess.check_output(["ping", "-c", "1", ip, "-i", "1"])
            except subprocess.CalledProcessError as e:
                _logs.append({"log_level": "ERROR", "log": _("Ping {ip} 失败, {output}").format(ip=ip, output=e.output)})
            else:
                _logs.append({"log_level": "INFO", "log": _("Ping {ip} 正常").format(ip=ip)})

        @translation.RespectsLanguage(language=get_language())
        def _check_package_url(url: str, _logs: list):
            # TODO 检测方案待讨论确认
            download_url = f"{url}/setup_agent.sh"
            try:
                response = requests.get(download_url, timeout=2)
            except requests.RequestException:
                _logs.append(
                    {
                        "log_level": "ERROR",
                        "log": _("{download_url} 检测下载失败，目标地址没有 setup_agent.sh 文件").format(download_url=download_url),
                    }
                )
            else:
                if response.status_code != 200:
                    _logs.append(
                        {
                            "log_level": "ERROR",
                            "log": _("{download_url} 检测下载失败").format(download_url=download_url),
                        }
                    )
                else:
                    _logs.append(
                        {
                            "log_level": "INFO",
                            "log": _("{download_url} 检测下载成功").format(download_url=download_url),
                        }
                    )

        @translation.RespectsLanguage(language=get_language())
        def _check_callback_url(url: str, _logs: list):
            _check_callback_url_return = cls.check_callback_url_reachable(url, raise_exception=False)
            if _check_callback_url_return["result"]:
                _logs.append({"log_level": "INFO", "log": _("回调地址 -> {url} 验证可达").format(url=url)})
            else:
                _logs.append(
                    {
                        "log_level": "ERROR",
                        "log": _("回调地址 -> {url} 不正确：{err_msg}").format(
                            url=url, err_msg=_check_callback_url_return["err_msg"]
                        ),
                    }
                )

        test_logs = []
        detect_hosts: Set[str] = set()
        for server in params.get("btfileserver", []) + params.get("dataserver", []) + params.get("taskserver", []):
            detect_hosts.add(server.get("inner_ip") or server.get("inner_ipv6"))

        with ThreadPoolExecutor(max_workers=settings.CONCURRENT_NUMBER) as ex:
            tasks = [ex.submit(_check_ip, detect_host, test_logs) for detect_host in detect_hosts]
            tasks.append(ex.submit(_check_package_url, params["package_inner_url"], test_logs))
            tasks.append(ex.submit(_check_package_url, params["package_outer_url"], test_logs))
            if params.get("outer_callback_url"):
                tasks.append(ex.submit(_check_callback_url, params["outer_callback_url"], test_logs))
            if params.get("callback_url"):
                tasks.append(ex.submit(_check_callback_url, params["callback_url"], test_logs))
            as_completed(tasks)
        test_result = True
        for log in test_logs:
            if log["log_level"] == "ERROR":
                test_result = False

        return test_result, test_logs

    class Meta:
        verbose_name = _("接入点（AccessPoint）")
        verbose_name_plural = _("接入点（AccessPoint）")


class Cloud(models.Model):
    """管控区域信息"""

    bk_cloud_id = models.IntegerField(primary_key=True)
    bk_cloud_name = models.CharField(max_length=45)
    isp = models.CharField(_("云服务商"), max_length=45, null=True, blank=True)
    ap_id = models.IntegerField(_("接入点ID"), null=True)
    gse_v1_ap_id = models.IntegerField(_("GSE1.0接入点ID"), null=True, blank=True)
    creator = JSONField(_("管控区域创建者"))

    is_visible = models.BooleanField(_("是否可见"), default=True)
    is_deleted = models.BooleanField(_("是否删除"), default=False)

    @classmethod
    def cloud_id_name_map(cls) -> Dict:
        all_cloud_map = {
            cloud.bk_cloud_id: cloud.bk_cloud_name for cloud in cls.objects.all().only("bk_cloud_id", "bk_cloud_name")
        }
        all_cloud_map[constants.DEFAULT_CLOUD] = str(_("直连区域"))
        return all_cloud_map

    class Meta:
        verbose_name = _("管控区域（BK-Net）")
        verbose_name_plural = _("管控区域（BK-Net）")


class InstallChannel(models.Model):
    """
    安装通道，利用 jump_servers 作为跳板登录到目标机器执行脚本，使用 upstream_servers 作为上游来渲染配置，
    由于各种特殊网络环境，目前安装通道需手动安装
    jump_servers = ["127.0.0.1", "127.0.0.2"]
    upstream_servers = {
        "taskserver": ["127.0.0.1"], "btfileserver": ["127.0.0.1"],"dataserver": ["127.0.0.1"],
        "agent_download_proxy": true, "channel_proxy_address": "http://127.0.0.1:17981"
    }
    """

    name = models.CharField(_("名称"), max_length=45)
    bk_cloud_id = models.IntegerField(_("管控区域ID"))
    jump_servers = JSONField(_("安装通道跳板机"))
    upstream_servers = JSONField(_("上游节点"))

    @classmethod
    def install_channel_id__host_objs_map(
        cls, install_channel_ids: Optional[List[int]] = None
    ) -> Dict[int, List["Host"]]:
        # 从数据库 Host 表中批量查询安装通道跳板机器的主机对象
        if install_channel_ids is not None:
            install_channels = cls.objects.filter(id__in=install_channel_ids)
        else:
            install_channels = cls.objects.all()

        result = defaultdict(list)

        # 计算出跳板机器归属的安装通道
        jump_server__install_channel_id_map: Dict[str, int] = {}
        filter_host_conditions = []
        for install_channel in install_channels:
            cloud_id = install_channel.bk_cloud_id
            for jump_server_ip in install_channel.jump_servers:
                jump_server__install_channel_id_map[f"{cloud_id}-{jump_server_ip}"] = install_channel.id
                filter_key = "inner_ipv6" if basic.is_v6(jump_server_ip) else "inner_ip"
                filter_host_conditions.append(Q(**{"bk_cloud_id": cloud_id, filter_key: jump_server_ip}))

        # 如果筛选条件为空，直接返回
        if not filter_host_conditions:
            return result

        # 得出跳板机的主机对象
        hosts = Host.objects.filter(bk_addressing=constants.CmdbAddressingType.STATIC.value).filter(
            reduce(operator.or_, filter_host_conditions)
        )
        host_key_obj_map = {}
        for host in hosts:
            for possible_key in [f"{host.bk_cloud_id}-{host.inner_ip}", f"{host.bk_cloud_id}-{host.inner_ipv6}"]:
                host_key_obj_map[possible_key] = host
        # 将主机对象映射回安装通道
        for jump_server_key, install_channel_id in jump_server__install_channel_id_map.items():
            host = host_key_obj_map.get(jump_server_key)
            if host:
                result[install_channel_id].append(host)

        return result

    class Meta:
        verbose_name = _("安装通道（InstallChannel）")
        verbose_name_plural = _("安装通道（InstallChannel）")


class Job(export_job_prometheus_mixin(), models.Model):
    """任务信息"""

    created_by = models.CharField(_("操作人"), max_length=45, default="")
    job_type = models.CharField(
        _("作业类型"), max_length=45, choices=constants.JOB_CHOICES, default=constants.JobType.INSTALL_PROXY
    )
    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    task_id_list = JSONField(_("任务ID列表"), default=list)
    start_time = models.DateTimeField(_("创建任务时间"), auto_now_add=True)
    end_time = models.DateTimeField(_("任务结束时间"), blank=True, null=True)
    status = models.CharField(
        _("任务状态"), max_length=45, choices=constants.JobStatusType.get_choices(), default=constants.JobStatusType.PENDING
    )
    global_params = JSONField(_("全局运行参数"), blank=True, null=True)
    statistics = JSONField(_("任务统计信息"), blank=True, null=True, default=dict)
    bk_biz_scope = JSONField(_("业务范围"))
    error_hosts = JSONField(_("发生错误的主机"))
    is_auto_trigger = models.BooleanField(_("是否为自动触发"), default=False)

    class Meta:
        verbose_name = _("任务信息（Job）")
        verbose_name_plural = _("任务信息（Job）")
        ordering = ["-id"]


class JobTask(models.Model):
    """主机和任务关联表，存储任务详情及结果"""

    job_id = models.IntegerField(_("作业ID"), db_index=True)
    bk_host_id = models.IntegerField(_("主机ID"), db_index=True)
    instance_id = models.CharField(_("实例ID"), max_length=45, db_index=True)
    pipeline_id = models.CharField(_("Pipeline节点ID"), max_length=50, default="", blank=True, db_index=True)
    status = models.CharField(max_length=45, choices=constants.STATUS_CHOICES, default=constants.StatusType.QUEUE)
    current_step = models.CharField(_("当前步骤"), max_length=45, default="")
    create_time = models.DateTimeField(auto_now_add=True, db_index=True)
    update_time = models.DateTimeField(auto_now=True, db_index=True)
    end_time = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = _("任务详情（JobTask）")
        verbose_name_plural = _("任务详情（JobTask）")


class GsePluginDesc(models.Model):
    """
    插件信息表
    """

    # 插件名需要全局唯一，防止冲突
    name = models.CharField(_("插件名"), max_length=32, unique=True, db_index=True)
    description = models.TextField(_("插件描述"))
    scenario = models.TextField(_("使用场景"))
    description_en = models.TextField(_("英文插件描述"), null=True, blank=True)
    scenario_en = models.TextField(_("英文使用场景"), null=True, blank=True)
    category = models.CharField(_("所属范围"), max_length=32, choices=constants.CATEGORY_CHOICES)
    launch_node = models.CharField(
        _("宿主节点类型要求"), max_length=32, choices=[("agent", "agent"), ("proxy", "proxy"), ("all", "all")], default="all"
    )

    config_file = models.CharField(_("配置文件名称"), max_length=128, null=True, blank=True)
    config_format = models.CharField(
        _("配置文件格式类型"),
        max_length=32,
        choices=constants.CONFIG_FILE_FORMAT_CHOICES,
        default="json",
        null=True,
        blank=True,
    )

    use_db = models.BooleanField(_("是否使用数据库"), default=0)
    auto_launch = models.BooleanField(_("是否在成功安装agent后自动拉起"), default=False)
    is_binary = models.BooleanField(_("是否二进制文件"), default=1)
    is_ready = models.BooleanField(_("是否启用插件"), default=True)

    deploy_type = models.CharField(
        _("部署方式"), choices=constants.DEPLOY_TYPE_CHOICES, max_length=64, null=True, blank=True
    )
    auto_type = models.IntegerField(
        _("托管类型"), choices=constants.GseAutoType.list_choices(), default=constants.GseAutoType.RESIDENT.value
    )
    source_app_code = models.CharField(_("来源系统APP CODE"), max_length=64, null=True, blank=True)

    node_manage_control = JSONField(_("节点管理管控插件信息"), null=True, blank=True)

    class Meta:
        verbose_name = _("插件信息（GsePluginDesc）")
        verbose_name_plural = _("插件信息（GsePluginDesc）")

    def __unicode__(self):
        return self.name

    @property
    def is_official(self):
        """
        是否为官方插件
        """
        return self.category == constants.CategoryType.official

    @classmethod
    def get_auto_launch_plugins(cls):
        return cls.objects.filter(auto_launch=True)

    def get_package_by_os(self, os, pkg_name):
        package = Packages.objects.get(
            project=self.name,
            os=os,
            pkg_name=pkg_name,
            cpu_arch__in=[constants.CpuType.x86_64, constants.CpuType.powerpc],
        )
        return package

    def get_control_by_os(self, os):
        control = ProcControl.objects.filter(project=self.name, os=os).order_by("id").last()
        return control

    def get_packages(self, version=None, os=None, cpu_arch=None):
        """
        按照版本、操作系统、CPU架构查询插件包记录。
        如果没有版本，则使用最新版本。
        :param version: str 版本号
        :param os: str 操作系统
        :param cpu_arch: str CPU架构
        :return: list[Packages]
        """
        query_params = {"project": self.name}

        if os is not None:
            query_params["os"] = os
        if cpu_arch is not None:
            query_params["cpu_arch"] = cpu_arch
        if version is not None:
            query_params["version"] = version

        return Packages.objects.filter(**query_params)

    @classmethod
    def get_plugins(cls):
        plugin_list_cache = cache.get("plugin_list")
        if plugin_list_cache:
            return plugin_list_cache

        plugin_list = list(GsePluginDesc.objects.filter(category="official").values_list("name", flat=True))
        cache.set("plugin_list", plugin_list, 300)
        return plugin_list

    @classmethod
    def list_packages(cls, md5_list, package_ids=None, query_params=None):
        """

        :param md5_list: list 各插件包的md5，用于校验
        :param package_ids: list 插件包的id，如果有这个参数，则query_params失效
        :param query_params: dict 使用插件
        :return:
        """
        query_params = copy.deepcopy(query_params)
        # 查询版本包，优先使用id查询插件包
        if package_ids:
            packages = Packages.objects.filter(id__in=package_ids)
        else:
            packages = cls(name=query_params.pop("name")).get_packages(**query_params)

        # 比对md5
        if "|".join(sorted(md5_list)) != "|".join(sorted([package.md5 for package in packages])):
            raise ValidationError("md5 not match")

        return packages

    @classmethod
    def batch_update_pkg(cls, md5_list, update_field_value_dict, package_ids=None, query_params=None):
        packages = cls.list_packages(md5_list, package_ids, query_params)
        # 修改属性
        packages.update(**update_field_value_dict)
        return packages


class Packages(models.Model):
    """
    插件更新包信息表
    """

    pkg_name = models.CharField(_("压缩包名"), max_length=128)
    version = models.CharField(_("版本号"), max_length=128)
    module = models.CharField(_("所属服务"), max_length=32)
    project = models.CharField(_("工程名"), max_length=32, db_index=True)
    pkg_size = models.IntegerField(_("包大小"))
    pkg_path = models.CharField(_("包路径"), max_length=128)
    md5 = models.CharField(_("md5值"), max_length=32)
    pkg_mtime = models.CharField(_("包更新时间"), max_length=48)
    pkg_ctime = models.CharField(_("包创建时间"), max_length=48)
    location = models.CharField(_("安装包链接"), max_length=512)
    os = models.CharField(
        _("系统类型"),
        max_length=32,
        choices=constants.PLUGIN_OS_CHOICES,
        default=constants.PluginOsType.linux,
        db_index=True,
    )
    cpu_arch = models.CharField(
        _("CPU类型"), max_length=32, choices=constants.CPU_CHOICES, default=constants.CpuType.x86_64, db_index=True
    )
    creator = models.CharField(_("操作人"), max_length=45, default="admin")

    is_release_version = models.BooleanField(_("是否已经发布版本"), default=True, db_index=True)
    # 由于创建记录时，文件可能仍然在传输过程中，因此需要标志位判断是否已经可用
    is_ready = models.BooleanField(_("插件是否可用"), default=True)

    version_log = models.TextField(_("版本日志"), null=True, blank=True)
    version_log_en = models.TextField(_("英文版本日志"), null=True, blank=True)

    @property
    def plugin_desc(self):
        if not hasattr(self, "_plugin_desc"):
            self._plugin_desc = GsePluginDesc.objects.get(name=self.project)
        return self._plugin_desc

    @property
    def proc_control(self):
        """
        获取进程控制信息
        """
        if not hasattr(self, "_proc_control"):
            self._proc_control = ProcControl.objects.get(plugin_package_id=self.id)
        return self._proc_control

    @classmethod
    def export_plugins(cls, project: str, version: str, os_type: str = None, cpu_arch: str = None) -> Dict[str, str]:
        """
        导出指定插件
        !!! 注意：该方法会有打包及同步等高延迟的动作，请勿在同步环境(uwsgi)下使用 !!!
        :param project: 导出的插件名
        :param version: 导出的插件版本
        :param cpu_arch: cpu类型
        :param os_type: 操作系统类型
        :return: {
            "file_path": ""/data/bkee/public/bk_nodeman/export/plugins-1.0.tgz
        } | raise Exception
        """
        filter_params = {"project": project, "version": version}
        if os_type is not None:
            filter_params["os"] = os_type
        if cpu_arch is not None:
            filter_params["cpu_arch"] = cpu_arch
        # 1. 确认需要导出的文件
        # 注意：未完成发布及nginx准备的插件不可导出
        package_objs = cls.objects.filter(**filter_params, is_ready=True, is_release_version=True)
        if not package_objs.exists():
            logger.error(
                "user try to export plugin project->[{project}] version->[{version}] "
                "filter_params->[{filter_params}] but is not exists, nothing will do.".format(
                    project=project, version=version, filter_params=filter_params
                )
            )
            raise ValueError(_("找不到可导出插件，请确认后重试"))

        # 临时的解压目录
        local_unzip_target_dir = files.mk_and_return_tmpdir()
        # 暂存导出插件的文件路径
        export_plugin_tmp_path = os.path.join(
            constants.TMP_DIR, constants.TMP_FILE_NAME_FORMAT.format(name=f"{uuid.uuid4().hex}.tgz")
        )

        # 2. 各个插件解压到指定的目录
        for package_obj in package_objs:
            package_obj.unzip(local_unzip_target_dir)
            logger.info(
                "package -> {pkg_name} os -> {os} cpu -> {cpu_arch} unzip success.".format(
                    pkg_name=package_obj.pkg_name, os=package_obj.os, cpu_arch=package_obj.cpu_arch
                )
            )

        # 3. 将解压的各个插件包打包成一个完整的插件
        with tarfile.open(export_plugin_tmp_path, "w:gz") as tar_file:
            # temp_path下的内容由于是从plugin处解压获得，所以应该已经符合external_plugins或者plugins的目录规范
            # 此处则不再指定
            tar_file.add(local_unzip_target_dir, ".")

        logger.debug(
            "export plugin -> {plugin_name} version -> {version} create export tmp path -> {export_plugin_tmp_path} "
            "from path-> {local_unzip_target_dir} success, ready to storage".format(
                plugin_name=project,
                version=version,
                export_plugin_tmp_path=export_plugin_tmp_path,
                local_unzip_target_dir=local_unzip_target_dir,
            )
        )

        # 4. 将导出的插件上传到存储源
        plugin_export_target_path = os.path.join(
            settings.EXPORT_PATH, f"{project}-{version}-{files.md5sum(name=export_plugin_tmp_path)}.tgz"
        )
        with open(export_plugin_tmp_path, mode="rb") as tf:
            storage_path = get_storage().save(plugin_export_target_path, tf)

        logger.info(
            "export done: plugin-> {plugin_name} version -> {version} export file -> {storage_path}".format(
                plugin_name=project, version=version, storage_path=storage_path
            )
        )

        # 清除临时文件
        os.remove(export_plugin_tmp_path)
        shutil.rmtree(local_unzip_target_dir)

        logger.info(
            "plugin -> {plugin_name} version -> {version} export job success.".format(
                plugin_name=project, version=version
            )
        )

        return {"file_path": storage_path}

    def unzip(self, local_target_dir: str) -> None:
        """
        将一个指定的插件解压到指定的目录下
        :param local_target_dir: 指定的解压目录
        :return: True | raise Exception
        """

        storage = get_storage()
        file_path = os.path.join(self.pkg_path, self.pkg_name)

        # 校验插件包是否存在
        if not storage.exists(file_path):
            logger.error(
                "try to unzip package-> {pkg_name} but file_path -> {file_path} is not exists, nothing will do.".format(
                    pkg_name=self.pkg_name, file_path=file_path
                )
            )
            raise ValueError(_("插件包不存在，请联系管理员处理"))

        # 将插件包解压到临时目录中
        package_tmp_dir = files.mk_and_return_tmpdir()
        # 文件的读取是从指定数据源（NFS或对象存储），可切换源模式，不直接使用原生open
        with storage.open(name=file_path, mode="rb") as tf_from_storage:
            with tarfile.open(fileobj=tf_from_storage) as tf:
                tf.extractall(path=package_tmp_dir)

        # 遍历插件包的一级目录，找出 PluginExternalTypePrefix 匹配的文件夹并加入到指定的解压目录
        # 一般来说，插件包是具体到机器操作系统类型的，所以 package_tmp_dir 下基本只有一个目录
        for external_type_prefix in os.listdir(package_tmp_dir):
            if external_type_prefix not in constants.PluginChildDir.get_optional_items():
                continue

            # 官方插件的插件包目录缺少 project，为了保证导出插件包可用，解压目标目录需要补充 project 层级
            dst_shims = [local_target_dir, f"{external_type_prefix}_{self.os}_{self.cpu_arch}"]
            if external_type_prefix == constants.PluginChildDir.OFFICIAL:
                dst_shims.append(self.project)

            # 将匹配的目录拷贝并格式化命名
            # 关于拷贝目录，参考：https://stackoverflow.com/questions/1868714/
            copy_tree(
                src=os.path.join(package_tmp_dir, external_type_prefix),
                dst=os.path.join(*dst_shims),
            )

        # 移除临时解压目录
        shutil.rmtree(package_tmp_dir)

        logger.info(
            "package-> {pkg_name} os -> {os} cpu_arch -> {cpu_arch} unzip to "
            "path -> {local_target_dir} success.".format(
                pkg_name=self.pkg_name, os=self.os, cpu_arch=self.cpu_arch, local_target_dir=local_target_dir
            )
        )

    class Meta:
        verbose_name = _("插件包（Packages）")
        verbose_name_plural = _("插件包（Packages）")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.project)

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [{self.pkg_name}|{self.version}], "
            f"selector -> [{self.project}|{self.os}|{self.cpu_arch}]>"
        )


class ProcControl(models.Model):
    """
    插件更新包信息表
    """

    module = models.CharField(_("模块名"), max_length=32)
    project = models.CharField(_("工程名"), max_length=32)
    plugin_package_id = models.IntegerField(_("记录对应的插件包ID"), default=0)

    install_path = models.TextField(_("安装路径"))
    log_path = models.TextField(_("日志路径"))
    data_path = models.TextField(_("数据文件路径"))
    pid_path = models.TextField(_("pid文件路径"))

    start_cmd = models.TextField(_("启动命令"), default="", blank=True)
    stop_cmd = models.TextField(_("停止命令"), default="", blank=True)
    restart_cmd = models.TextField(_("重启命令"), default="", blank=True)
    reload_cmd = models.TextField(_("重载命令"), default="", blank=True)
    kill_cmd = models.TextField(_("kill命令"), default="", blank=True)
    version_cmd = models.TextField(_("进程版本查询命令"), default="", blank=True)
    health_cmd = models.TextField(_("进程健康检查命令"), default="", blank=True)
    debug_cmd = models.TextField(_("调试进程命令"), default="", blank=True)

    os = models.CharField(
        _("系统类型"), max_length=32, choices=constants.PLUGIN_OS_CHOICES, default=constants.PluginOsType.linux
    )
    process_name = models.CharField(_("实际二进制执行文件名"), max_length=128, null=True, default=None)
    port_range = models.TextField(_("插件允许使用的端口范围，格式 1,3,6-8,10-100"), null=True, blank=True, default="")
    need_delegate = models.BooleanField(_("是否需要托管"), default=True)

    @property
    def listen_port_required(self):
        """
        该插件是否需要监听端口
        """
        return bool(self.port_range)

    @classmethod
    def parse_port_num(cls, port_num):
        """
        检查端口号是否合法
        """
        if isinstance(port_num, six.string_types) and port_num.strip().isdigit():
            port_num = int(port_num)
        elif isinstance(port_num, int):
            pass
        else:
            raise ValueError(_("无法解析的端口号：%s") % port_num)

        if 1 <= port_num <= 65535:
            return port_num

        raise ValueError(_("不在合法范围内的端口号：%s") % port_num)

    @classmethod
    def parse_port_range(cls, port_range_str):
        """
        解析
        :param port_range_str: 端口范围字符串
        :return: 二元组列表，元组的两个元素分别代表开
        """

        # 为空直接返回
        if not port_range_str:
            return []

        port_range_list = []

        try:
            # 以逗号拆开
            range_str_list = port_range_str.split(",")
            for range_str in range_str_list:
                try:
                    # 先判断是不是单个数字
                    port_num = cls.parse_port_num(range_str)
                    # 如果是单个数字，则转化为区间并保存
                    port_range_list.append((port_num, port_num))
                except Exception:
                    # 如果不是单个数字，尝试识别为区间字符串
                    port_range_tuple = range_str.split("-")

                    # 尝试拆分为上界和下界
                    if len(port_range_tuple) != 2:
                        raise ValueError(_("不合法的端口范围定义格式：%s") % range_str)

                    # 对上界和下界分别进行解析
                    port_num_min, port_num_max = port_range_tuple
                    port_num_min = cls.parse_port_num(port_num_min)
                    port_num_max = cls.parse_port_num(port_num_max)

                    if port_num_min > port_num_max:
                        # 下界 > 上界 也是不合法的范围
                        raise ValueError(_("不合法的端口范围定义格式：%s") % range_str)
                    port_range_list.append((port_num_min, port_num_max))

        except Exception as e:
            raise ValueError(_("端口范围字符串解析失败：%s") % e)

        return port_range_list

    class Meta:
        verbose_name = _("插件控制表（ProcControl）")
        verbose_name_plural = _("插件控制表（ProcControl）")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.project)


class UploadPackage(models.Model):
    """
    上传文件记录
    """

    file_name = models.CharField(_("上传包文件名"), max_length=64, db_index=True)
    module = models.CharField(_("模块名"), max_length=32)

    file_path = models.CharField(_("文件上传的路径名"), max_length=128)
    file_size = models.IntegerField(_("文件大小，单位Byte"))
    md5 = models.CharField(_("文件MD5"), max_length=32)

    upload_time = models.DateTimeField(_("文件上传时间"), auto_now_add=True)
    creator = models.CharField(_("上传用户名"), max_length=64)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    class Meta:
        verbose_name = _("文件包上传记录")
        verbose_name_plural = _("文件包上传记录表")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.file_name)

    @classmethod
    def create_record(cls, module, file_path, md5, operator, source_app_code, file_name, is_file_copy=False):
        """
        创建一个新的上传记录
        :param module: 文件模块
        :param file_path: 文件源路径
        :param md5: 文件MD5
        :param operator: 操作者
        :param source_app_code: 上传来源APP_CODE
        :param file_name: 期望的文件保存名，在非文件覆盖的情况下，该名称不是文件最终的保存名
        :param is_file_copy: 是否复制而非剪切文件，适应初始化内置插件需要使用
        :return: upload record
        """
        # 注意：MD5参数值将会直接使用，因为服务器上的MD5是由nginx协助计算，应该在views限制

        try:
            storage = get_storage()
            # 判断文件是否已经存在
            if not storage.exists(file_path):
                logger.warning(
                    "operator -> {operator} try to create record for file -> {file_path} but is not exists.".format(
                        operator=operator, file_path=file_path
                    )
                )
                raise CreateRecordError(_("文件{file_path}不存在，请确认后重试").format(file_path=file_path))

            target_file_path = os.path.join(settings.UPLOAD_PATH, file_name)

            # 如果读写路径一致无需拷贝文件，此处 target_file_path / file_path 属于同个文件源
            if target_file_path != file_path:
                with storage.open(name=file_path, mode="rb") as fs:
                    # 不允许覆盖同名文件的情况下，文件名会添加随机串，此时 target_file_path / file_name 应刷新
                    target_file_path = storage.save(name=target_file_path, content=fs)
                    file_name = os.path.basename(target_file_path)

                # 如果是通过 mv 拷贝到指定目录，此时原文件应该删除
                if not is_file_copy:
                    storage.delete(file_path)

            record = cls.objects.create(
                file_name=file_name,
                module=module,
                file_path=target_file_path,
                file_size=storage.size(target_file_path),
                md5=md5,
                upload_time=timezone.now(),
                creator=operator,
                source_app_code=source_app_code,
            )

        except Exception:
            logger.error(
                "failed to mv source_file -> {file_path} to target_file_path -> {target_file_path}, "
                "err_msg -> {err_msg}".format(
                    file_path=file_path, target_file_path=target_file_path, err_msg=traceback.format_exc()
                )
            )
            raise CreateRecordError(_("文件迁移失败，请联系管理员协助处理"))

        logger.info(
            "new record for file -> {file_path} module -> {module} is added by operator -> {operator} "
            "from system -> {source_app_code}.".format(
                file_path=file_path, module=module, operator=operator, source_app_code=source_app_code
            )
        )

        return record


class DownloadRecord(models.Model):
    """
    下载任务记录
    """

    # 任务状态枚举
    TASK_STATUS_READY = 0
    TASK_STATUS_DOING = 1
    TASK_STATUS_SUCCESS = 2
    TASK_STATUS_FAILED = 3

    TASK_STATUS_CHOICES = (
        (TASK_STATUS_READY, _("任务准备中")),
        (TASK_STATUS_DOING, _("任务进行中")),
        (TASK_STATUS_SUCCESS, _("任务已完成")),
        (TASK_STATUS_FAILED, _("任务失败")),
    )

    # 插件类型枚举
    CATEGORY_GSE_PLUGIN = "gse_plugin"

    CATEGORY_CHOICES = ((CATEGORY_GSE_PLUGIN, _("gse插件")),)

    CATEGORY_TASK_DICT = {CATEGORY_GSE_PLUGIN: Packages.export_plugins}

    category = models.CharField(_("下载文件类型"), max_length=32, choices=CATEGORY_CHOICES)
    query_params = models.CharField(_("下载查询参数"), max_length=256)

    file_path = models.CharField(_("打包后的文件路径"), max_length=256)
    task_status = models.IntegerField(_("任务状态"), default=0, choices=TASK_STATUS_CHOICES)
    error_message = models.TextField(_("任务错误信息"))

    creator = models.CharField(_("任务创建者"), max_length=64)
    create_time = models.DateTimeField(_("下载任务创建时间"), auto_now_add=True)
    finish_time = models.DateTimeField(_("任务完成时间"), auto_now=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    @property
    def is_finish(self):

        return self.task_status == self.TASK_STATUS_FAILED or self.task_status == self.TASK_STATUS_SUCCESS

    @property
    def is_failed(self):

        return self.task_status == self.TASK_STATUS_FAILED

    @property
    def download_params(self):
        """
        构造下载参数
        :return: id=xx&key=xxx
        """
        info_dict = {"job_id": self.id, "key": self.download_key}

        return six.moves.urllib.parse.urlencode(info_dict)

    @property
    def download_key(self):
        """
        下载验证key，防止恶意遍历下载文件
        :return:
        """
        # 由于下载的文件路径不会对外暴露，可以通过这个信息进行MD5校验
        md5 = hashlib.md5()
        md5.update(self.file_path.encode())

        return md5.hexdigest()

    @classmethod
    def create_record(cls, category: str, query_params: Dict[str, Any], creator: str, source_app_code: str):
        """
        创建下载任务记录
        :param category: 下载文件类型
        :param query_params: 下载查询参数
        :param creator: 任务创建者
        :param source_app_code: 请求来源蓝鲸系统代号
        :return: download record
        """

        record = cls.objects.create(
            category=category,
            query_params=json.dumps(query_params),
            file_path="",
            task_status=cls.TASK_STATUS_READY,
            error_message="",
            creator=creator,
            create_time=timezone.now(),
            source_app_code=source_app_code,
        )
        logger.info(
            "download record -> {record_id} is create from app -> {source_app_code} for category -> {category} "
            "query_params -> {query_params}".format(
                record_id=record.id, source_app_code=source_app_code, category=category, query_params=query_params
            )
        )

        return record

    def execute(self):
        """
        执行一个任务
        :return: True | raise Exception
        """
        task_status = self.TASK_STATUS_SUCCESS
        error_message = ""

        try:
            task_func = self.CATEGORY_TASK_DICT.get(self.category)
            self.task_status = self.TASK_STATUS_DOING
            self.save()

            # 直接利用请求的参数调用
            result = task_func(**json.loads(self.query_params))

            self.file_path = result["file_path"]

        except Exception as error:
            logger.error(
                "failed to execute task -> {record_id} for -> {err_msg}".format(
                    record_id=self.id, err_msg=traceback.format_exc()
                )
            )

            task_status = self.TASK_STATUS_FAILED
            error_message = _("任务失败: {err_msg}").format(err_msg=error)

            six.raise_from(error, error)

        finally:
            self.task_status = task_status
            self.error_message = error_message
            self.finish_time = timezone.now()
            self.save()
            logger.info(
                "task -> {record_id} is done with status -> {task_status} error_message -> {err_msg}".format(
                    record_id=self.id, task_status=self.task_status, err_msg=self.error_message
                )
            )

    class Meta:
        verbose_name = _("下载记录")
        verbose_name_plural = _("下载记录")


class PluginResourcePolicy(models.Model):
    plugin_name = models.CharField(_("插件名"), max_length=32, db_index=True)
    cpu = models.IntegerField(_("CPU限额"), default=constants.PLUGIN_DEFAULT_CPU_LIMIT)
    mem = models.IntegerField(_("内存限额"), default=constants.PLUGIN_DEFAULT_MEM_LIMIT)
    bk_biz_id = models.IntegerField(_("业务ID"), db_index=True)
    bk_obj_id = models.CharField(_("CMDB对象ID"), max_length=32, db_index=True)
    bk_inst_id = models.IntegerField(_("CMDB实例ID"), db_index=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("插件资源设置")
        verbose_name_plural = _("插件资源设置")
        unique_together = (("plugin_name", "bk_obj_id", "bk_inst_id"),)


class PluginConfigTemplate(models.Model):
    """
    插件配置文件模板
    """

    plugin_name = models.CharField(_("插件名"), max_length=32, db_index=True)
    plugin_version = models.CharField(_("版本号"), max_length=128, db_index=True)
    name = models.CharField(_("配置模板名"), max_length=128, db_index=True)
    version = models.CharField(_("配置模板版本"), max_length=128, db_index=True)
    is_main = models.BooleanField(_("是否主配置"), default=False, db_index=True)

    format = models.CharField(_("文件格式"), max_length=16)
    file_path = models.CharField(_("文件在该插件目录中相对路径"), max_length=128)
    content = models.TextField(_("配置内容"))
    is_release_version = models.BooleanField(_("是否已经发布版本"), db_index=True)

    creator = models.CharField(_("创建者"), max_length=64)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    os = models.CharField(
        _("操作系统"), max_length=16, choices=constants.PLUGIN_OS_CHOICES, default=constants.PluginOsType.linux
    )
    cpu_arch = models.CharField(
        _("CPU架构"), max_length=16, choices=constants.CPU_CHOICES, default=constants.CpuType.x86_64
    )

    class Meta:
        verbose_name = _("插件配置文件模板")
        verbose_name_plural = _("插件配置文件模板表")
        # 唯一性限制
        unique_together = (
            # 对于同一个插件的同一个版本，同名配置文件只能存在一个
            ("plugin_name", "plugin_version", "name", "version", "is_main", "os", "cpu_arch"),
        )

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [{self.name}|{self.version}], "
            f"selector -> [{self.plugin_name}|{self.plugin_version}|{self.os}|{self.cpu_arch}]>"
        )

    def create_instance(self, data, creator=None, source_app_code=None):
        """
        返回 PluginConfigInstance 实例
        对于同一系统创建的实例，若data的MD5相同，则直接复用
        """
        json_data = json.dumps(data, sort_keys=True)
        hash_md5 = hashlib.md5(json_data.encode())
        data_md5 = hash_md5.hexdigest()

        # 创建配置文件实例
        instance = PluginConfigInstance.objects.filter(
            plugin_config_template=self.id,
            source_app_code=source_app_code or self.source_app_code,
            data_md5=data_md5,
        ).first()
        if not instance:
            instance = PluginConfigInstance.objects.create(
                plugin_config_template=self.id,
                source_app_code=source_app_code or self.source_app_code,
                data_md5=data_md5,
                render_data=json_data,
                creator=creator or self.creator,
            )
        else:
            instance.render_data = json_data
            instance.creator = creator or self.creator
            instance.save(update_fields=["render_data", "creator"])

        return instance

    def render(self, context):
        # 先用context渲染自己，把内部参数都渲染上
        context = nested_render_data(context, context)

        # 如果是拨测或远程采集，需要渲染ip，此时需要注入函数
        context = self.render_function(context)

        rendered_config = nested_render_data(self.content, context)
        return rendered_config

    def render_function(self, context):
        """
        函数注入
        目前仅用于拨测任务
        """
        context["get_hosts_by_node"] = get_hosts_by_node
        return context


class PluginConfigInstance(models.Model):
    """
    插件配置文件实例
    """

    plugin_config_template = models.IntegerField(_("对应实例记录ID"), db_index=True)
    render_data = models.TextField(_("渲染参数"))
    data_md5 = models.CharField(_("渲染参数MD5"), max_length=50, default="", db_index=True)
    creator = models.CharField(_("创建者"), max_length=64, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64, db_index=True)

    def render_name(self, name):
        """
        使用render_data渲染参数
        :param name: 名称参数
        :return: 渲染后的结果
        """
        template = Template(name)
        try:
            render_data = json.loads(self.render_data)
        except BaseException as e:
            logger.error(f"render_data is not a dict: {e}")
            render_data = self.render_data

        if isinstance(render_data, dict):
            return template.render(render_data)
        else:
            return name

    def render_config_template(self, extra_context=None):
        # 添加内置变量
        extra_context = extra_context or {}
        render_data = json.loads(self.render_data)

        # 使用模板引擎渲染用户参数
        render_data = nested_render_data(render_data, extra_context)

        # 先用 extra_context 去渲染 render_data 本身
        context = copy.deepcopy(extra_context)
        context.update(render_data)

        # 如果是拨测或远程采集，需要渲染ip，此时需要注入函数
        context = self.render_function(context)

        template_content = self.jinja_template.render(context)
        return template_content

    def render_function(self, context):
        """
        函数注入
        目前仅用于拨测任务
        """
        context["get_hosts_by_node"] = get_hosts_by_node
        return context

    @property
    def jinja_template(self):
        if not hasattr(self, "_jinja_template"):
            self._jinja_template = Template(self.template.content)
        return self._jinja_template

    @property
    def template(self):
        if not hasattr(self, "_template"):
            self._template = PluginConfigTemplate.objects.get(id=self.plugin_config_template)
        return self._template

    class Meta:
        verbose_name = _("插件配置文件实例")
        verbose_name_plural = _("插件配置文件实例表")


class SubscriptionStep(models.Model):
    """订阅步骤"""

    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    index = models.IntegerField(_("顺序"), default=0)
    step_id = models.CharField(_("步骤ID"), max_length=64, db_index=True)
    type = models.CharField(_("步骤类型"), max_length=20)
    config = JSONField(_("配置"))
    params = JSONField(_("参数"))

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.get_subscription(self.subscription_id, show_deleted=True)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        # if self.subscription_id != value.id:
        #     self.subscription_id = value.id
        #     self.save()
        self._subscription = value

    class Meta:
        verbose_name = _("订阅步骤")
        verbose_name_plural = _("订阅步骤")
        ordering = ["index"]
        unique_together = (("subscription_id", "step_id"),)

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [{self.type}|{self.step_id}], "
            f"selector -> [sub_id:{self.subscription_id}]>"
        )


class Subscription(export_subscription_prometheus_mixin(), orm.SoftDeleteModel):
    """订阅"""

    class ObjectType(object):
        HOST = "HOST"
        SERVICE = "SERVICE"

    OBJECT_TYPE_CHOICES = (
        (ObjectType.HOST, _("主机")),
        (ObjectType.SERVICE, _("服务")),
    )

    class NodeType(object):
        TOPO = "TOPO"
        INSTANCE = "INSTANCE"
        SERVICE_TEMPLATE = "SERVICE_TEMPLATE"
        SET_TEMPLATE = "SET_TEMPLATE"

    NODE_TYPE_CHOICES = (
        (NodeType.TOPO, _("动态实例（拓扑）")),
        (NodeType.INSTANCE, _("静态实例")),
        (NodeType.SERVICE_TEMPLATE, _("服务模板")),
        (NodeType.SET_TEMPLATE, _("集群模板")),
    )

    class CategoryType(object):
        POLICY = "policy"
        DEBUG = "debug"
        ONCE = "once"

    CATEGORY_ALIAS_MAP = {
        CategoryType.DEBUG: _("调试"),
        CategoryType.POLICY: _("策略"),
        CategoryType.ONCE: _("一次性订阅"),
    }

    ROOT = -1

    name = models.CharField(_("任务名称"), max_length=64, null=True, blank=True)
    bk_biz_id = models.IntegerField(_("业务ID"), db_index=True, null=True)
    object_type = models.CharField(_("对象类型"), max_length=20, choices=OBJECT_TYPE_CHOICES, db_index=True)
    node_type = models.CharField(_("节点类型"), max_length=20, choices=NODE_TYPE_CHOICES, db_index=True)
    nodes = JSONField(_("节点"), default=list)
    instance_selector = JSONField(_("订阅任务范围主机属性筛选"), null=True, blank=True)
    target_hosts = JSONField(_("下发的目标机器"), default=None, null=True)
    from_system = models.CharField(_("所属系统"), max_length=30)
    update_time = models.DateTimeField(_("更新时间"), auto_now=True, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    creator = models.CharField(_("操作人"), max_length=64, db_index=True)
    enable = models.BooleanField(default=True, db_index=True)
    is_main = models.BooleanField(default=False, db_index=True)
    category = models.CharField(_("订阅类别"), max_length=32, null=True, blank=True, db_index=True)
    plugin_name = models.CharField(_("插件名称"), max_length=64, null=True, blank=True, db_index=True)
    bk_biz_scope = JSONField(_("业务范围"), default=list)

    pid = models.BigIntegerField(_("父订阅ID"), default=ROOT, db_index=True)

    objects = orm.SoftDeleteModelManager()

    @property
    def steps(self):
        if not getattr(self, "_steps", None):
            self._steps = list(SubscriptionStep.objects.filter(subscription_id=self.id))
            for step in self._steps:
                # 设置 subscription 属性，减少查询次数
                step.subscription = self
        self._steps = sorted(self._steps, key=lambda x: x.index)
        return self._steps

    @steps.setter
    def steps(self, value):
        self._steps = value

    @property
    def scope(self):
        need_register = False
        if self.node_type == self.NodeType.INSTANCE and self.object_type == self.ObjectType.HOST:
            # nodes 中的信息未注册到CMDB，需要注册
            for node in self.nodes:
                if "instance_info" in node:
                    need_register = True
        return {
            "bk_biz_id": self.bk_biz_id,
            "object_type": self.object_type,
            "node_type": self.node_type,
            "nodes": self.nodes,
            "need_register": need_register,
            "instance_selector": self.instance_selector,
        }

    @classmethod
    def filter_parent_qs(cls) -> QuerySet:
        """返回父订阅的QuerySet"""
        return cls.objects.filter(pid=cls.ROOT)

    @classmethod
    def get_subscriptions(cls, ids, show_deleted=False):
        subscriptions = cls.objects.filter(id__in=ids, show_deleted=show_deleted)
        subscription_steps = SubscriptionStep.objects.filter(
            subscription_id__in=[subscription.id for subscription in subscriptions]
        )
        subscription_step_dict = defaultdict(list)
        for step in subscription_steps:
            subscription_step_dict[step.subscription_id].append(step)

        for subscription in subscriptions:
            subscription._steps = subscription_step_dict[subscription.id]

        return subscriptions

    @classmethod
    def get_subscription(cls, subscription_id: int, show_deleted=False):
        try:
            subscription = cls.objects.get(id=subscription_id, show_deleted=show_deleted)
        except cls.DoesNotExist:
            raise SubscriptionNotExist({"subscription_id": subscription_id})
        return subscription

    def is_running(self, instance_id_list: List[str] = None):
        """订阅下是否有运行中的任务"""
        base_kwargs = {"subscription_id": self.id, "is_latest": True}
        if instance_id_list is not None:
            base_kwargs["instance_id__in"] = instance_id_list
        status_set = set(SubscriptionInstanceRecord.objects.filter(**base_kwargs).values_list("status", flat=True))
        # 任务已执行完成（有明确结果），直接返回
        if status_set & {constants.JobStatusType.PENDING, constants.JobStatusType.RUNNING}:
            return True
        return False

    @classmethod
    def get_host_id__bk_obj_sub_map(
        cls, bk_host_ids: Union[List[int], Set[int]], plugin_name: str, is_latest=True
    ) -> Dict[int, List[Dict]]:
        """
        根据插件名称查询已存在的订阅策略中，插件状态所属的CMDB对象映射表，用于计算策略抑制关系
        :param bk_host_ids: 主机ID列表
        :param plugin_name: 插件名称
        :param is_latest: 仅考虑策略的实际管控（部署成功）的主机
        :return:
        """
        host_id__bk_obj_sub_map = defaultdict(list)
        # 本插件已存在的订阅策略，用于计算策略抑制关系
        exist_subscriptions = cls.objects.filter(
            category=cls.CategoryType.POLICY, plugin_name=plugin_name, enable=True
        ).only("id", "name", "create_time")
        exist_subscription_id__obj_map = {sub.id: sub for sub in exist_subscriptions}
        # 经过讨论，策略管控范围仅计入实际生效归属（即is_latest=True）
        # 对于策略暂时无法管控的情况（例如巡检未拉起，Agent异常）允许一次性任务及其他低优先级策略操作，直到最高优先级策略成功管控
        # is_latest=False对策略而言表示主机已脱离实际管控，存在的意义在于，策略仅开放整体停用，巡检等方式定期移除已不再范围内的
        # ProcessStatus，对于保留下来的记录，可以表示"待管控"的候选意义，通过is_latest=False筛选并比较策略间的拉起优先级
        exist_process_statuses = ProcessStatus.objects.filter(
            source_id__in=list(exist_subscription_id__obj_map.keys()),
            bk_host_id__in=bk_host_ids,
            name=plugin_name,
            is_latest=is_latest,
        ).values("bk_host_id", "source_id", "bk_obj_id")

        # 主机插件对应的范围层级列表映射
        for proc_status in exist_process_statuses:
            host_id__bk_obj_sub_map[proc_status["bk_host_id"]].append(
                {
                    "bk_obj_id": proc_status["bk_obj_id"],
                    "subscription": exist_subscription_id__obj_map.get(int(proc_status["source_id"]))
                    # "subscription_id": int(proc_status.source_id),
                    # "name": exist_subscription_id__obj_map.get(int(proc_status.source_id)),
                }
            )
        return host_id__bk_obj_sub_map

    def check_is_suppressed(
        self,
        action: str,
        cmdb_host_info: Dict[str, Any],
        topo_order: List[str],
        host_id__bk_obj_sub_map: Dict[int, Any],
    ) -> Dict:
        """
        计算被抑制的订阅实例是否被抑制，并返回订阅实例在指定订阅中的CMDB对象ID
        :param action: 实例执行动作
        :param cmdb_host_info: cmdb的host结构，所需字段：bk_biz_id bk_cloud_id bk_host_innerip bk_host_id
        :param topo_order:
        :param host_id__bk_obj_sub_map: 主机插件对应的CMDB对象及订阅映射
            {
                1: [{
                    "bk_obj_id": "biz",
                    "subscription": Subscription,
                }],
                2: [{
                    "bk_obj_id": "host",
                    "subscription": Subscription,
                }]
            }
        :return:
        """

        def _cal_priority(_bk_obj_sub: Dict) -> int:
            """计算主机部署策略优先级"""
            _bk_obj_id = _bk_obj_sub["bk_obj_id"]
            _priority = topo_order.index(_bk_obj_id) if _bk_obj_id in topo_order else -1
            return _priority

        def _bk_obj_sub_comparator(_left: Dict, _right: Dict) -> int:
            """
            主机部署策略优先级比较器
            优先级比已存在的小，则认为被抑制
            相同优先级时，新策略抑制旧策略（通过创建时间判断）
            """
            # _left < _right 返回-1，在sorted体现升序
            if _cal_priority(_left) < _cal_priority(_right) or (
                _cal_priority(_left) == _cal_priority(_right)
                and _left["subscription"].create_time < _right["subscription"].create_time
            ):
                return -1
            return 1

        def _get_ordered_bk_obj_subs(_bk_obj_subs: List[Dict]) -> List[Dict]:
            """获取优先级升序的主机部署策略列表"""
            _sorted_bk_obj_subs = sorted(_bk_obj_subs, key=cmp_to_key(_bk_obj_sub_comparator))
            return _sorted_bk_obj_subs

        def _fetch_bk_obj_subs_by_host_id(_bk_host_id) -> List[Dict]:
            """根据主机ID获取主机部署策略列表"""
            _bk_obj_subs = []
            for _bk_obj_sub in host_id__bk_obj_sub_map.get(_bk_host_id, []):
                # 跳过当前订阅：
                if _bk_obj_sub["subscription"].id == self.id:
                    continue
                _bk_obj_subs.append(_bk_obj_sub)
            return _bk_obj_subs

        def _get_sub_inst_bk_obj_id() -> Optional[str]:
            """获取订阅实例目标匹配的拓扑层级"""

            # 订阅全为主机节点，表明目标匹配的拓扑层级为HOST，直接返回
            if self.node_type == self.NodeType.INSTANCE and self.object_type == self.ObjectType.HOST:
                return constants.CmdbObjectId.HOST

            # 订阅实例目标匹配的拓扑层级
            _sub_inst_bk_obj_id = None

            _bk_biz_id = cmdb_host_info["bk_biz_id"]

            for sub_scope in self.nodes:
                # 单独处理业务层级
                if (
                    sub_scope.get("bk_obj_id") == constants.CmdbObjectId.BIZ
                    and sub_scope.get("bk_inst_id") == _bk_biz_id
                ):
                    _sub_inst_bk_obj_id = constants.CmdbObjectId.BIZ

            for bk_obj_id in topo_order:
                if bk_obj_id in [constants.CmdbObjectId.BIZ, constants.CmdbObjectId.HOST]:
                    # 业务和主机单独处理
                    continue

                # 其它层级统一处理
                bk_inst_ids = cmdb_host_info.get(bk_obj_id, [])
                for sub_scope in self.nodes:
                    # 找出实例在scope中所属节点的bk_obj_id
                    if sub_scope.get("bk_obj_id") == bk_obj_id and sub_scope.get("bk_inst_id") in bk_inst_ids:
                        _sub_inst_bk_obj_id = bk_obj_id

            # 最低层级一定是主机
            for sub_scope in self.nodes:
                # 单独处理业务层级，兼容 bk_host_id 和 IP+管控区域两种模式
                if ("bk_host_id" in sub_scope and sub_scope["bk_host_id"] == cmdb_host_info["bk_host_id"]) or (
                    "bk_cloud_id" in sub_scope
                    and "ip" in sub_scope
                    and sub_scope["bk_cloud_id"] == cmdb_host_info["bk_cloud_id"]
                    and sub_scope["ip"] == cmdb_host_info["bk_host_innerip"]
                ):
                    _sub_inst_bk_obj_id = constants.CmdbObjectId.HOST
            return _sub_inst_bk_obj_id

        def _construct_return_data(
            _is_suppressed: bool,
            _sub_inst_bk_obj_id: Optional[str] = None,
            _highest_priority_target: Optional[Dict[str, Any]] = None,
            _ordered_bk_obj_subs: Optional[List[Dict[str, Any]]] = None,
        ):
            if not _is_suppressed:
                return {
                    "is_suppressed": _is_suppressed,
                    "sub_inst_bk_obj_id": _sub_inst_bk_obj_id,
                    "ordered_bk_obj_subs": _ordered_bk_obj_subs,
                }

            _highest_priority_sub = _highest_priority_target["subscription"]
            return {
                "is_suppressed": True,
                "category": self.category,
                "sub_inst_bk_obj_id": _sub_inst_bk_obj_id,
                "ordered_bk_obj_subs": _ordered_bk_obj_subs,
                "suppressed_by": {
                    "name": _highest_priority_sub.name,
                    "category": _highest_priority_sub.category,
                    "subscription_id": _highest_priority_sub.id,
                    "bk_obj_id": _highest_priority_target["bk_obj_id"],
                },
            }

        # TODO 非策略、非一次性订阅任务的其他订阅无需考虑抑制情况，是否校验其他订阅存在部署主配置的情况等待讨论
        if not self.category:
            return _construct_return_data(_is_suppressed=False)

        # 一次性订阅仅 「安装 / 更新插件」（Action相同）需要计算抑制关系，其他动作允许策略中豁免
        if self.category == self.CategoryType.ONCE and action not in [constants.JobType.MAIN_INSTALL_PLUGIN]:
            return _construct_return_data(_is_suppressed=False)

        # 获取订阅实例目标匹配的拓扑层级
        sub_inst_bk_obj_id = _get_sub_inst_bk_obj_id()

        if self.category == self.CategoryType.ONCE:
            """
            一次性订阅的优先级规则：
            「主机所属订阅均为一次性订阅」「主机所属订阅为空」 新订阅抑制旧订阅
            「主机所属订阅包含策略类型」被所有策略类型的订阅抑制
            综上：由于 host_id__bk_obj_sub_map 仅查询「策略」，当主机归属策略不为空时，主机所属一次性订阅被抑制
            简述：一次性订阅不能操作被「策略」覆盖的主机
            """
            bk_obj_subs = _fetch_bk_obj_subs_by_host_id(cmdb_host_info["bk_host_id"])
            if not bk_obj_subs:
                return _construct_return_data(False, sub_inst_bk_obj_id)

            # 主机已被策略部署，一次性订阅被抑制，返回被最高优先级策略抑制的信息
            ordered_bk_obj_subs = _get_ordered_bk_obj_subs(bk_obj_subs)
            highest_priority_target = ordered_bk_obj_subs[-1]
            return _construct_return_data(
                True, sub_inst_bk_obj_id, highest_priority_target, _ordered_bk_obj_subs=ordered_bk_obj_subs
            )

        # 根据当前订阅实例构造一个bk_obj_sub
        current_bk_obj_sub = {"subscription": self, "bk_obj_id": sub_inst_bk_obj_id}
        # 整合已部署的主机策略信息及当前策略进行优先级排序
        ordered_bk_obj_subs = _get_ordered_bk_obj_subs(
            _fetch_bk_obj_subs_by_host_id(cmdb_host_info["bk_host_id"]) + [current_bk_obj_sub]
        )

        highest_priority_target = ordered_bk_obj_subs[-1]
        highest_priority_sub = highest_priority_target["subscription"]
        # 当前实例的部署策略不是最高优先级，返回被抑制情况
        if highest_priority_sub.id != self.id:
            return _construct_return_data(
                True, sub_inst_bk_obj_id, highest_priority_target, _ordered_bk_obj_subs=ordered_bk_obj_subs
            )
        return _construct_return_data(False, sub_inst_bk_obj_id, _ordered_bk_obj_subs=ordered_bk_obj_subs)

    class Meta:
        verbose_name = _("订阅（Subscription）")
        verbose_name_plural = _("订阅（Subscription）")

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [enable:{self.enable}|category:{self.category}], "
            f"selector -> [{self.object_type}|{self.node_type}|{self.from_system}]>"
        )


class SubscriptionTask(models.Model):
    """订阅执行任务"""

    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    scope = JSONField(_("执行范围"))
    actions = JSONField(_("不同step执行的动作名称。键值对"))
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    err_msg = models.TextField(_("错误信息"), blank=True, null=True)
    is_ready = models.BooleanField(_("是否准备就绪"), default=False)
    is_auto_trigger = models.BooleanField(_("是否为自动触发"), default=False)
    pipeline_id = models.CharField(_("Pipeline ID"), max_length=50, default="", blank=True, db_index=True)

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.objects.get(id=self.subscription_id)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        if self.subscription_id != value.id:
            self.subscription_id = value.id
            self.save()
        self._subscription = value

    @property
    def instance_records(self):
        return SubscriptionInstanceRecord.objects.filter(task_id=self.id)

    class Meta:
        verbose_name = _("订阅任务")
        verbose_name_plural = _("订阅任务")
        ordering = ["-create_time"]

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [is_ready:{self.is_ready}|is_auto_trigger:{self.is_auto_trigger}], "
            f"selector -> [sub_id:{self.subscription_id}|pipeline_id:{self.pipeline_id}]>"
        )


class SubscriptionInstanceRecord(models.Model):
    """订阅任务的实例执行记录"""

    id = models.BigAutoField(primary_key=True)
    task_id = models.IntegerField(_("任务ID"), db_index=True)
    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    instance_id = models.CharField(_("实例ID"), max_length=128, db_index=True)
    instance_info = JSONField(_("实例信息"))
    steps = JSONField(_("步骤信息"))
    pipeline_id = models.CharField(_("Pipeline ID"), max_length=50, default="", blank=True, db_index=True)
    start_pipeline_id = models.CharField(_("Start Pipeline ID"), max_length=50, default="", blank=True, db_index=True)
    update_time = models.DateTimeField(_("更新时间"), default=timezone.now, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    need_clean = models.BooleanField(_("是否需要清洗临时信息"), default=False)
    is_latest = models.BooleanField(_("是否为实例最新记录"), default=True, db_index=True)

    status = models.CharField(
        _("任务状态"), max_length=45, choices=constants.JobStatusType.get_choices(), default=constants.JobStatusType.PENDING
    )

    @property
    def subscription_task(self):
        if not hasattr(self, "_subscription_task"):
            self._subscription_task = SubscriptionTask.objects.get(id=self.task_id)
        return self._subscription_task

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.objects.get(id=self.subscription_id)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        if self.subscription_id != value.id:
            self.subscription_id = value.id
            self.save()
        self._subscription = value

    def save_pipeline(self, pipeline_id, pipeline_tree):
        """
        设置pipeline属性
        """
        PipelineTree.objects.update_or_create(id=pipeline_id, defaults={"tree": pipeline_tree})
        self.pipeline_id = pipeline_id
        self.save()

    def run_pipeline(self):
        pipeline_tree = PipelineTree.objects.get(id=self.pipeline_id)
        pipeline_tree.run()

    def init_steps(self):
        """
        初始化步骤数据
        """
        step_data = [
            {"id": step.step_id, "type": step.type, "pipeline_id": "", "action": None, "extra_info": {}}
            for step in self.subscription.steps
        ]
        self.steps = step_data

    def get_all_step_data(self):
        if not self.steps:
            self.init_steps()

        return self.steps

    def get_step_data(self, step_id):
        """
        根据 step_id 获取步骤数据
        """
        if not self.steps:
            self.init_steps()

        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise KeyError(_("步骤ID [{}] 在该订阅配置中不存在").format(step_id))

    def set_step_data(self, step_id, data):
        """
        根据 step_id 设置步骤数据
        """
        if not self.steps:
            self.init_steps()

        # 不允许修改 id 和 type
        data.pop("id", None)
        data.pop("type", None)

        steps = copy.deepcopy(self.steps)

        for step in steps:
            if step["id"] != step_id:
                continue

            # 更新步骤数据，保存
            step.update(data)
            self.steps = steps
            return

        raise KeyError(_("步骤ID [{}] 在该订阅配置中不存在").format(step_id))

    def simple_instance_info(self):
        instance_info = self.instance_info
        return {
            "host": {
                key: instance_info["host"].get(key)
                for key in instance_info["host"]
                if key
                in [
                    "bk_host_innerip_v6",
                    "bk_host_innerip",
                    "bk_cloud_id",
                    "bk_supplier_account",
                    "bk_host_name",
                    "bk_host_id",
                    "bk_biz_id",
                    "bk_biz_name",
                    "bk_cloud_name",
                ]
            },
            "service": {
                key: instance_info["service"].get(key)
                for key in (instance_info.get("service") or {})
                if key in ["id", "name", "bk_module_id", "bk_host_id"]
            },
        }

    class Meta:
        verbose_name = _("订阅实例记录")
        verbose_name_plural = _("订阅实例记录")


class JobSubscriptionInstanceMap(models.Model):
    job_instance_id = models.BigIntegerField(_("作业实例ID"), db_index=True)
    subscription_instance_ids = JSONField(_("订阅实例ID列表"), default=list)
    node_id = models.CharField(_("节点ID"), max_length=32, db_index=True)
    status = models.CharField(_("作业状态"), max_length=45, default=constants.BkJobStatus.PENDING)

    class Meta:
        verbose_name = _("作业平台ID映射表")
        verbose_name_plural = _("作业平台ID映射表")


class SubscriptionInstanceLog(models.Model):
    subscription_instance_record_id = models.BigIntegerField(_("订阅实例ID"), db_index=True)
    log = models.TextField(_("日志内容"))
    level_name = models.SlugField(_("日志等级"), max_length=32)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("订阅实例日志")
        verbose_name_plural = _("订阅实例日志")


class AutoDateTimeField(models.DateTimeField):
    def pre_save(self, model_instance, add):
        return timezone.now()


class SubscriptionInstanceStatusDetail(models.Model):
    id = models.BigAutoField(primary_key=True)
    subscription_instance_record_id = models.BigIntegerField(_("订阅实例ID"), db_index=True)
    node_id = models.CharField(_("Pipeline原子ID"), max_length=50, default="", blank=True, db_index=True)
    status = models.CharField(
        _("任务状态"), max_length=45, choices=constants.JobStatusType.get_choices(), default=constants.JobStatusType.RUNNING
    )
    log = models.TextField(_("日志内容"))
    update_time = models.DateTimeField(_("更新时间"), null=True, blank=True, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("订阅实例状态表")
        verbose_name_plural = _("订阅实例状态表")


class CmdbEventRecord(models.Model):
    """记录CMDB事件回调"""

    bk_biz_id = models.IntegerField(_("订阅ID"), db_index=True)
    subscription_id = models.CharField(_("实例ID"), max_length=50, db_index=True)
    event_type = models.CharField(_("事件类型"), max_length=20)
    action = models.CharField(_("动作"), max_length=20)
    obj_type = models.CharField(_("对象类型"), max_length=32)
    data = JSONField(_("实例信息"))
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        verbose_name = _("CMDB事件记录")
        verbose_name_plural = _("CMDB事件记录")


class PipelineTree(models.Model):
    """
    记录Pipeline树 ID 与 拓扑的对应信息
    """

    id = models.CharField(_("PipelineID"), primary_key=True, max_length=32)
    tree = LazyJSONField(_("Pipeline拓扑树"))

    def run(self, priority=None):
        # 根据流程描述结构创建流程对象
        parser = PipelineParser(pipeline_tree=self.tree)
        pipeline = parser.parse()
        if priority is not None:
            action_result = task_service.run_pipeline(pipeline, priority=priority)
        else:
            action_result = task_service.run_pipeline(pipeline)

        if not action_result.result:
            raise PipelineExecuteFailed({"msg": action_result.message})


class ResourceWatchEvent(models.Model):
    """
    资源监听事件
    """

    EVENT_TYPE_CHOICE = (
        ("update", "update"),
        ("create", "create"),
        ("delete", "delete"),
    )

    bk_cursor = models.CharField(_("游标"), max_length=128, primary_key=True)
    bk_event_type = models.CharField(_("事件类型"), max_length=32, choices=EVENT_TYPE_CHOICE)
    bk_resource = models.CharField(_("资源"), max_length=32)
    bk_detail = JSONField(_("事件详情"), default=dict)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        verbose_name = _("CMDB资源监听事件")
        verbose_name_plural = _("CMDB资源监听事件")

    def __str__(self):
        return (
            f"<{self.__class__.__name__}({self.pk}) "
            f"info -> [{self.bk_event_type}|{self.bk_resource}|{self.bk_detail.get('bk_biz_id')}]>"
        )


class GseConfigEnv(models.Model):
    env_value = models.JSONField(_("环境变量值"))
    agent_name = models.CharField(_("Agent名称"), max_length=32)
    version = models.CharField(_("版本号"), max_length=128)
    cpu_arch = models.CharField(
        _("CPU类型"), max_length=32, choices=constants.CPU_CHOICES, default=constants.CpuType.x86_64, db_index=True
    )
    os = models.CharField(
        _("系统类型"),
        max_length=32,
        choices=constants.PLUGIN_OS_CHOICES,
        default=constants.PluginOsType.linux,
        db_index=True,
    )

    class Meta:
        verbose_name = _("安装包环境变量")
        verbose_name_plural = _("安装包环境变量")

    def __str__(self) -> str:
        return f"{self.version}-{self.agent_name}-{self.os}-{self.cpu_arch}"


class GseConfigTemplate(models.Model):
    name = models.CharField(_("配置文件名称"), max_length=32)
    content = models.TextField(_("配置内容"))
    agent_name = models.CharField(_("Agent名称"), max_length=32)
    version = models.CharField(_("版本号"), max_length=128)
    cpu_arch = models.CharField(
        _("CPU类型"), max_length=32, choices=constants.CPU_CHOICES, default=constants.CpuType.x86_64, db_index=True
    )
    os = models.CharField(
        _("系统类型"),
        max_length=32,
        choices=constants.PLUGIN_OS_CHOICES,
        default=constants.PluginOsType.linux,
        db_index=True,
    )

    @property
    def env_values(self) -> GseConfigEnv:
        if not getattr(self, "_env_values", None):
            self._env_values = GseConfigEnv.objects.get(
                os=self.os, version=self.version, cpu_arch=self.cpu_arch, agent_name=self.agent_name
            )
        return self._env_values

    class Meta:
        verbose_name = _("安装包模板表")
        verbose_name_plural = _("安装包模板表")

    def __str__(self) -> str:
        return f"{self.name}-{self.version}-{self.agent_name}-{self.os}-{self.cpu_arch}"
