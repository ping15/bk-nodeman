<template>
  <article class="agent-setup" v-test="'setupWrapper'">
    <!--左侧表单信息-->
    <section class="agent-setup-left">
      <tips class="mb20">
        <template #default>
          <i18n path="Agent安装要求tips" tag="p">
            Agent
            <bk-link class="tips-link" theme="primary" @click="handleShowPanel">{{ $t('安装要求') }}</bk-link>
            <bk-link class="tips-link" theme="primary" @click="handleShowSetting">{{ $t('表格展示设置') }}</bk-link>
          </i18n>
        </template>
      </tips>
      <div class="setup-form">
        <bk-form ref="form" class="mb30" :model="formData" :rules="rules" v-test="'formSetup'">
          <install-method :is-manual="isManual" required @change="installMethodHandle"></install-method>
          <bk-form-item error-display-type="normal" property="bk_biz_id" :label="$t('安装到业务')" required>
            <bk-biz-select
              class="content-basic"
              v-model="formData.bk_biz_id"
              :show-select-all="false"
              :multiple="false"
              :auto-update-storage="false"
              :clearable="false"
              @change="handleBizChange">
            </bk-biz-select>
          </bk-form-item>
          <bk-form-item error-display-type="normal" :label="$t('管控区域')" property="bk_cloud_id" required>
            <permission-select
              :class="['content-basic', { 'is-error': ['no_proxy', 'overdue'].includes(proxyStatus) }]"
              searchable
              :permission="permissionSwitch"
              :permission-type="'cloud_view'"
              :permission-key="'view'"
              :placeholder="$t('选择管控区域')"
              :loading="loadingCloudList"
              :option-list="bkCloudList"
              :option-id="'bk_cloud_id'"
              :option-name="'bk_cloud_name'"
              v-model="formData.bk_cloud_id"
              @change="handleCloudChange"
              @toggle="handleCloudToggle">
            </permission-select>
            <p v-show="proxyCount === 1" class="item-warning-tip">{{ $t('proxy数量提示') }}</p>
            <i18n
              :path="i18nPath"
              class="form-error-tip item-error-tips"
              tag="p"
              v-show="['no_proxy', 'overdue'].includes(proxyStatus)">
              <span
                class="btn"
                @click="handleGotoProxy">
                {{ proxyStatus === 'overdue' ? $t('前往更新') : $t('前往安装') }}
              </span>
            </i18n>
          </bk-form-item>
          <bk-form-item
            property="install_channel_id"
            error-display-type="normal"
            :label="$t('安装通道')"
            :desc="{ content: $t('安装通道Desc'), placements: ['right'], width: 220 }"
            required>
            <bk-select
              class="content-basic"
              v-model="formData.install_channel_id"
              :clearable="false"
              :disabled="channelDisabled"
              :loading="loadingChannelList"
              @change="handleChannelChange">
              <bk-option v-for="item in filterChannelList" :key="item.id" :id="item.id" :name="item.name"></bk-option>
            </bk-select>
          </bk-form-item>
          <bk-form-item
            error-display-type="normal"
            property="ap_id"
            :label="$t('接入点')"
            required>
            <bk-select
              class="content-basic"
              v-model="formData.ap_id"
              v-bk-tooltips="{
                delay: [300, 0],
                content: $t('接入点已在管控区域中设定'),
                disabled: !apDisabled,
                placement: 'right'
              }"
              :clearable="false"
              :disabled="apDisabled || isEmptyCloud"
              :loading="loadingApList">
              <!-- 直连区域 - 非默认通道 禁用自动选择接入点 -->
              <bk-option
                v-for="item in curApList"
                :key="item.id"
                :id="item.id"
                :name="item.name"
                :disabled="item.id === -1 && formData.install_channel_id !== 'default'" />
            </bk-select>
          </bk-form-item>
          <bk-form-item :label="$t('安装信息')" required>
            <filter-ip-tips
              class="filter-tips"
              v-if="filterList.length && showFilterTips"
              @click="handleShowDetail">
            </filter-ip-tips>
          </bk-form-item>
          <bk-form-item class="form-item-vertical" :class="['mt0', { 'mb30': isScroll }]">
            <InstallTable
              :class="{ 'agent-setup-table': isManual }"
              ref="installTable"
              :local-mark="`agent_steup${isManual ? '_manual' : ''}`"
              :is-manual="isManual"
              :setup-info="setupInfo"
              :extra-params="extraParams"
              :bk-cloud-id="formData.bk_cloud_id"
              auto-sort
              @add="handleAddItem"
              @delete="handleDeleteItem">
            </InstallTable>
          </bk-form-item>
        </bk-form>
        <div class="form-btn" :class="{ 'fixed': isScroll, 'shrink': isScroll && showRightPanel }">
          <bk-button
            theme="primary"
            ext-cls="nodeman-primary-btn"
            @click="handleSetup"
            :loading="loadingSetupBtn"
            v-test.common="'formCommit'">
            <div class="form-btn-install">
              <span>{{ $t('安装') }}</span>
              <span class="num">{{ setupNum }}</span>
            </div>
          </bk-button>
          <bk-button class="nodeman-cancel-btn ml10" @click="handleCancel">{{ $t('取消') }}</bk-button>
        </div>
      </div>
    </section>
    <!--右侧提示信息-->
    <section class="agent-setup-right" :class="{ 'right-panel': showRightPanel }">
      <right-panel v-model="showRightPanel" :host-type="hostType" :host-list="hostList"></right-panel>
      <!-- <right-panel v-model="showRightPanel" :list="tipsList" :title="$t('安装要求')"></right-panel> -->
    </section>
    <!--过滤ip信息-->
    <filter-dialog v-model="showFilterDialog" :list="filterList" :title="$t('忽略详情')"></filter-dialog>
  </article>
</template>
<script lang="ts">
import { Component, Ref, Mixins, Watch } from 'vue-property-decorator';
import { AgentStore, CloudStore, MainStore } from '@/store/index';
import Tips from '@/components/common/tips.vue';
import RightPanel from '@/components/common/right-panel-tips.vue';
import InstallTable from '@/components/setup-table/install-table.vue';
import InstallMethod from '@/components/common/install-method.vue';
import FilterIpTips from '@/components/common/filter-ip-tips.vue';
import mixin from '@/components/common/filter-ip-mixin';
import formLabelMixin from '@/common/form-label-mixin';
import FilterDialog from '@/components/common/filter-dialog.vue';
import PermissionSelect from '@/components/common/permission-select.vue';
import getTipsTemplate from '../config/tips-template';
import { setupTableConfig, parentHead, setupDiffConfigs } from '../config/setupTableConfig';
import { addListener, removeListener } from 'resize-detector';
import { debounce, isEmpty, deepClone, getManualConfig } from '@/common/util';
import { IApExpand } from '@/types/config/config';
import { IAgent } from '@/types/agent/agent-type';
import { ISetupHead, ISetupRow, ISetupParent } from '@/types';
import { regIPv6 } from '@/common/regexp';
import { reguRequired } from '@/common/form-check';
import { getDefaultConfig } from '@/config/config';

@Component({
  name: 'agent-setup',
  components: {
    Tips,
    RightPanel,
    InstallTable,
    InstallMethod,
    FilterIpTips,
    FilterDialog,
    PermissionSelect,
  },
})

export default class AgentSetup extends Mixins(mixin, formLabelMixin) {
  @Ref('form') private readonly form!: any;
  @Ref('installTable') private readonly installTable!: any;

  // 是否为安装方式
  private isManual = false;
  // 表单数据
  private formData: IAgent = {
    bk_biz_id: MainStore.selectedBiz.length === 1 ? MainStore.selectedBiz[0] : '',
    bk_cloud_id: '',
    install_channel_id: '',
    ap_id: '',
  };
  private rules = {
    bk_biz_id: [reguRequired],
    bk_cloud_id: [reguRequired],
    install_channel_id: [reguRequired],
    ap_id: [reguRequired],
  };
  // 右侧提示面板是否显示
  private showRightPanel = false;
  // agent安装信息表格
  private setupInfo: { header: ISetupHead[], data: ISetupRow[], parentHead: ISetupParent[] } = {
    header: setupTableConfig,
    parentHead,
    data: [],
  };
  // 监听界面滚动
  private listenResize: any = null;
  private isScroll = false;
  // 安装按钮加载中的状态
  private loadingSetupBtn = false;
  // 管控区域列表加载状态
  private loadingCloudList = false;
  // 接入点列表加载状态
  private loadingApList = false;
  private loadingChannelList = false;
  // 安装信息数量
  private setupNum = 1;
  private apList: IApExpand[] = [];
  // 接入点是否只读
  private apDisabled = false;
  // proxy数量
  private proxyCount = 0;
  // Proxy状态
  private proxyStatus = '';
  // Proxy所在管控区域ID
  private proxyCloudId: undefined | number = undefined;

  private get permissionSwitch() {
    return MainStore.permissionSwitch;
  }
  private get selectedBiz() {
    return MainStore.selectedBiz;
  }
  // 当前管控区域列表
  private get bkCloudList() {
    return AgentStore.cloudList.filter(item => !item.bk_biz_scope
            || item.bk_biz_scope.includes(this.formData.bk_biz_id));
  }
  private get i18nPath() {
    if (this.proxyStatus === 'no_proxy') {
      return 'Proxy未安装';
    }
    return 'Proxy过期';
  }
  private get curApList() {
    if (this.isManual
        || (this.formData.bk_cloud_id === window.PROJECT_CONFIG.DEFAULT_CLOUD && this.apList.length === 2)) {
      return AgentStore.apList.filter(item => item.id !== -1);
    }
    return this.apList;
  }
  private get isNotAutoSelect() {
    return AgentStore.apList.length === 1;
  }
  // 右侧面板提示信息
  private get tipsList() {
    return getTipsTemplate({ apUrl: AgentStore.apUrl, net: this.isDefaultCloud });
  }
  private get isEmptyCloud() {
    return isEmpty(this.formData.bk_cloud_id);
  }
  private get isDefaultCloud() {
    return window.PROJECT_CONFIG.DEFAULT_CLOUD === this.formData.bk_cloud_id;
  }
  // 编辑态额外参数 - 安装类型切换时会丢失已填数据
  private get extraParams() {
    return this.isManual ? ['port', 'account', 'auth_type', 'password', 'prove'] : ['password', 'prove'];
  }
  private get hostType() {
    if (isEmpty(this.formData.bk_cloud_id)) {
      return 'mixed';
    } if (this.isDefaultCloud) {
      return 'agent';
    }
    return 'Pagent';
  }
  private get hostList() {
    const curCloud = AgentStore.cloudList.find(item => item.bk_cloud_id === this.formData.bk_cloud_id);
    return this.setupInfo.data.map(item => ({
      bk_cloud_id: this.formData.bk_cloud_id,
      bk_cloud_name: curCloud ? curCloud.bk_cloud_name : '',
      inner_ip: item ? item.inner_ip : '',
      ap_id: this.formData.ap_id,
    }));
  }
  private get channelDisabled() {
    return isEmpty(this.formData.bk_cloud_id);
  }
  private get filterChannelList() {
    return AgentStore.channelList.filter(item => item.id === 'default' || item.bk_cloud_id === this.formData.bk_cloud_id);
  }

  @Watch('formData.ap_id')
  public handleApIdChange(val: number) {
    let urlType = '';
    if (isEmpty(this.formData.bk_cloud_id)) {
      urlType = '';
    } else if (this.isDefaultCloud) {
      urlType = 'package_inner_url';
    } else {
      urlType = 'package_outer_url';
    }
    AgentStore.setApUrl({
      id: val,
      urlType,
    });
  }
  private created() {
    this.handleInit();
  }
  private mounted() {
    this.listenResize = debounce(300, () => this.handleResize());
    addListener(this.$el as HTMLElement, this.listenResize);
    this.initLabelWidth(this.form);
  }
  private beforeDestroy() {
    AgentStore.setApUrl({ id: '', urlType: '' });
    removeListener(this.$el as HTMLElement, this.listenResize);
  }


  public handleInit() {
    this.initApList();
    this.initCloudList();
    this.initChannelList();
  }
  public async initApList() {
    this.loadingApList = true;
    this.apList = await AgentStore.getApList();
    this.loadingApList = false;
  }
  public async initCloudList() {
    this.loadingCloudList = true;
    await AgentStore.getCloudList({ RUN_VER: window.PROJECT_CONFIG.RUN_VER });
    this.loadingCloudList = false;
  }
  public async initChannelList() {
    this.loadingChannelList = true;
    await CloudStore.getChannelList();
    this.loadingChannelList = false;
  }
  /**
   * 监听界面滚动
   */
  public handleResize() {
    // 60：三级导航的高度  52： 一级导航高度
    this.isScroll = this.$el.scrollHeight + 60 > this.$root.$el.clientHeight - 52;
  }
  /**
   * 获取表单数据
   */
  public getFormData() {
    const { bk_biz_id, bk_cloud_id, ap_id, install_channel_id: channelId } = this.formData;
    return this.installTable.getData().map((item: ISetupRow) => ({
      ...item,
      bk_biz_id,
      bk_cloud_id,
      ap_id,
      install_channel_id: channelId === 'default' ? null : channelId,
      is_manual: this.isManual,
    }));
  }
  /**
   * 开始安装agent
   */
  public handleSetup() {
    const setupTableValidate = this.installTable.validate();
    this.form.validate().then(async () => {
      if (setupTableValidate) {
        this.loadingSetupBtn = true;
        this.showFilterTips = false;
        const params = {
          job_type: 'INSTALL_AGENT',
          hosts: this.getFormData().map(({ prove, ...item }: ISetupRow) => {
            if (isEmpty(item.login_ip)) {
              delete item.login_ip;
            }
            if (isEmpty(item.bt_speed_limit)) {
              delete item.bt_speed_limit;
            } else {
              item.bt_speed_limit = Number(item.bt_speed_limit);
            }
            const authType = item.auth_type?.toLowerCase() as ('key' | 'password');
            if (item[authType]) {
              item[authType] = this.$safety.encrypt(item[authType] as string);
            }
            item.peer_exchange_switch_for_agent = Number(item.peer_exchange_switch_for_agent);
            if (this.$DHCP && regIPv6.test(item.inner_ip as string)) {
              item.inner_ipv6 = item.inner_ip;
              delete item.inner_ip;
            }
            return item;
          }),
        };
        const res = await AgentStore.installAgentJob({
          params,
          config: {
            needRes: true,
            globalError: false,
          },
        });
        this.loadingSetupBtn = false;
        if (!res) return;

        if (res.result || res.code === 3801018) {
          // 部分忽略
          // mixin: handleFilterIp 处理过滤IP信息
          this.handleFilterIp(res.data);
        } else if (res.code === 3801013) {
          // Proxy过期或者未安装
          const firstItem = res.data.ip_filter.find((item: any) => ['no_proxy', 'overdue'].includes(item.exception))
            || {};
          this.proxyStatus = firstItem.exception;
          this.proxyCloudId = firstItem.bk_cloud_id;
        } else {
          const message = res.message ? res.message : this.$t('请求出错');
          this.$bkMessage({
            message,
            delay: 3000,
            theme: 'error',
          });
        }
      }
    })
      .catch(() => {});
  }
  /**
   * 取消安装Agent
   */
  public handleCancel() {
    this.$router.push({ name: 'agentStatus' });
  }
  /**
   * 添加
   */
  public handleAddItem() {
    this.setupNum += 1;
  }
  /**
   * 删除
   */
  public handleDeleteItem() {
    this.setupNum -= 1;
  }
  /**
     * 业务变更
     */
  public handleBizChange() {
    this.formData.bk_cloud_id = '';
    MainStore.updateEdited(true);
  }
  /**
   * 管控区域变更
   */
  public handleCloudChange(value: number) {
    MainStore.updateEdited(true);
    const item = this.bkCloudList.find(item => item.bk_cloud_id === value);
    if (!this.isManual && item && item.bk_cloud_id !== window.PROJECT_CONFIG.DEFAULT_CLOUD) {
      this.apDisabled = true;
      this.formData.ap_id = item.ap_id;
      // /* eslint-disable @typescript-eslint/prefer-optional-chain */
      // this.proxyStatus = item && item.proxy_count ? '' : 'no_proxy'
      // this.proxyCloudId = item && item.proxy_count ? undefined : item.bk_cloud_id
      // /* eslint-enable @typescript-eslint/prefer-optional-chain */
    } else {
      this.apDisabled = false;
      this.formData.ap_id = this.curApList.length ? this.curApList[0].id : -1;
    }
    if (value !== window.PROJECT_CONFIG.DEFAULT_CLOUD) {
      this.installTable.updateRow((row: ISetupRow) => {
        if (row.os_type === 'WINDOWS') {
          row.port = getDefaultConfig(row.os_type, 'port', 445);
        }
      });
    }
    // eslint-disable-next-line @typescript-eslint/prefer-optional-chain
    this.proxyCount = item && item.proxy_count ? item.proxy_count : 0;
    this.formData.install_channel_id = this.filterChannelList.length === 1 ? 'default' : '';
  }
  public handleCloudToggle(toggle: boolean) {
    if (toggle) {
      this.proxyStatus = '';
    }
  }
  public handleChannelChange(value: number | string) {
    MainStore.updateEdited(true);
    // 非直连管控区域存在已绑定的接入点
    if (value !== 'default' && this.formData.bk_cloud_id === window.PROJECT_CONFIG.DEFAULT_CLOUD) {
      this.formData.ap_id = this.curApList.find(item => item.id !== -1)?.id || '';
    }
  }
  /**
   * 安装方式变更
   */
  public installMethodHandle(isManual = false) {
    this.isManual = isManual;
    const apList: IApExpand[] = deepClone(this.apList);
    if (this.isManual) {
      this.setupInfo.header = getManualConfig(setupTableConfig, setupDiffConfigs);
      // 手动安装无自动选择
      this.apList = apList.filter(item => item.id !== -1);
      if (this.formData.ap_id === -1) {
        // 自动接入点改默认接入点
        const apDefault = this.isNotAutoSelect ? this.apList[0] : this.apList.find(item => item.is_default);
        // 替换为默认接入点
        this.formData.ap_id = apDefault ? apDefault.id : '';
      }
      // 手动安装可以自由选择接入点
      this.apDisabled = false;
    } else {
      this.setupInfo.header = setupTableConfig;
      if (!apList.find(item => item.id === -1)) {
        const obj = {
          id: -1,
          name: this.$t('自动选择'),
        };
        apList.unshift(obj as IApExpand);
        this.apList = apList;
      }
      this.handleCloudChange(this.formData.bk_cloud_id);
    }
    this.setupInfo.data = deepClone(this.installTable.getData());
    this.installTable.handleInit();
    this.installTable.handleScroll();
  }
  /**
   * 跳转Proxy界面
   */
  public handleGotoProxy() {
    if (!this.proxyStatus || isEmpty(this.proxyCloudId)) return;
    switch (this.proxyStatus) {
      case 'no_proxy': {
        const installRouteData = this.$router.resolve({
          name: 'setupCloudManager',
          params: {
            type: 'create',
            title: window.i18n.t('nav_安装Proxy'),
            id: this.proxyCloudId,
          },
        });
        window.open(installRouteData.href, '_blank');
        break;
      }
      case 'overdue': {
        const detailRouteData = this.$router.resolve({
          name: 'cloudManagerDetail',
          params: {
            id: this.proxyCloudId,
          },
        });
        window.open(detailRouteData.href, '_blank');
        break;
      }
    }
  }
  public handleShowPanel() {
    this.setupInfo.data = this.getFormData();
    this.showRightPanel = true;
  }
  public handleCreateCloud() {
    this.$router.push({ name: 'cloudManager' });
  }
  public handleShowSetting() {
    this.installTable.handleToggleSetting(true);
  }
}
</script>
<style lang="postcss" scoped>
@import "@/css/mixins/nodeman.css";

>>> .bk-select {
  background-color: #fff;
  &.is-disabled {
    background-color: #fafbfd;
  }
}
>>> .input-type .bk-form-control {
  height: 32px;
}
>>> .setup-body-wrapper {
  overflow: initial;
}
.agent-setup {
  @mixin layout-flex row;
  &-left {
    flex: 1;
    /* >>> .agent-setup-table {
      max-width: 1200px;
    } */
    >>> .install-table-body {
      overflow: visible;
    }
    .content-basic {
      width: 480px;
      &.is-error {
        border-color: #ff5656;
      }
    }
    .item-error-tips .btn {
      color: #3a84ff;
      cursor: pointer;
      &:hover {
        color: #699df4;
      }
    }
    .item-warning-tip {
      margin: 2px 0 0;
      line-height: 18px;
      font-size: 12px;
      color: #ff9c01;
    }
    .form-btn {
      transition: right .2s;

      @mixin layout-flex row, center, center;
      &-install {
        @mixin layout-flex row, center, center;
        .num {
          margin-left: 8px;
          padding: 0 6px;
          height: 16px;
          border-radius: 8px;
          background: #e1ecff;
          color: #3a84ff;
          font-weight: bold;
          font-size: 12px;

          @mixin layout-flex row, center, center;
        }
      }
      &.fixed {
        position: fixed;
        height: 54px;
        left: 60px;
        bottom: 0;
        right: 0;
        background: #fff;
        box-shadow: 0px -3px 6px 0px rgba(49,50,56,.05);
        z-index: 100;
        border-top: 1px solid #e2e2e2;
      }
    }
    .filter-tips {
      height: 32px;
    }
  }
  &-right {
    width: 6px;
    transition: width .2s;
  }
}
</style>
