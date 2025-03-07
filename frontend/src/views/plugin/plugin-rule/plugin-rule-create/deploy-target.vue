<template>
  <bk-form v-test.policy="'targetForm'" ref="computedForm" :model="formData" :rules="rules">
    <bk-form-item
      v-if="isCreateType"
      class="rule-name"
      v-test.policy="'name'"
      :label="isGrayRule ? $t('灰度名称') : $t('策略名称')"
      property="policyName"
      required>
      <bk-input :placeholder="$t('请输入策略名称')" v-model.trim="formData.policyName" ref="ruleNameRef"></bk-input>
    </bk-form-item>
    <bk-form-item
      :label="isGrayRule ? $t('灰度目标') : $t('部署目标')"
      class="selector-form"
      required>
      <!-- <IpSelect
        class="ip-selector"
        v-test.policy="'ipSelect'"
        :action="['strategy_create', 'strategy_view']"
        :node-type="isGrayRule ? 'INSTANCE' : targetPreview.node_type"
        :checked-data="targetPreview.nodes"
        :customize-limit="isGrayRule"
        @check-change="handleTargetChange">
      </IpSelect> -->
      <IpSelector
        class="ip-selector"
        v-test.policy="'ipSelect'"
        :panel-list="panelList"
        :value="selectorNodes"
        :action="'strategy_create'"
        @change="handleTargetChange">
      </IpSelector>
    </bk-form-item>
    <bk-form-item>
      <bk-button
        class="nodeman-primary-btn"
        theme="primary"
        v-test.common="'stepNext'"
        :disabled="!targetPreview.nodes || !targetPreview.nodes.length || !formData.policyName"
        @click="handleNext">
        {{ $t('下一步') }}
      </bk-button>
      <bk-button
        class="nodeman-cancel-btn ml5"
        @click="handleStepCancel">
        {{ $t('取消') }}
      </bk-button>
    </bk-form-item>
  </bk-form>
</template>
<script lang="ts">
import { Component, Ref, Emit, Prop, Vue } from 'vue-property-decorator';
// import IpSelect from '@/components/ip-selector/business/topo-selector-nodeman.vue';
import IpSelector, { ISelectorValue, toSelectorNode, toStrategyNode } from '@/components/common/nm-ip-selectors';
import { PluginStore } from '@/store';
import { INodeType } from '@/types/plugin/plugin-type';
import { reguRequired, reguFnStrLength } from '@/common/form-check';

@Component({
  name: 'deploy-target',
  components: {
    // IpSelect,
    IpSelector,
  },
})
export default class DeployTarget extends Vue {
  @Prop({ type: Number, default: 1 }) private readonly step!: number;
  @Ref('computedForm') private readonly computedForm!: any;
  @Ref('ruleNameRef') private readonly ruleNameRef!: any;

  private formData: Dictionary = {
    policyName: PluginStore.strategyData.name || '',
  };
  private rules: Dictionary = {
    policyName: [reguRequired, reguFnStrLength()],
  };
  private stepChanged = false;

  private get isGrayRule() {
    return PluginStore.isGrayRule;
  }
  private get isCreateType() {
    return PluginStore.isCreateType;
  }
  private get targetPreview() {
    return PluginStore.strategyData.scope || {};
  }
  private get selectorNodes() {
    const { nodes, node_type } = PluginStore.strategyData.scope;
    return {
      host_list: node_type === 'INSTANCE' ? toSelectorNode(nodes, node_type) : [],
      node_list: node_type === 'TOPO' ? toSelectorNode(nodes, node_type) : [],
    };
  }
  private get panelList() {
    return this.isGrayRule
      ? ['staticTopo', 'manualInput']
      : ['dynamicTopo', 'staticTopo', 'manualInput'];
  }

  private mounted() {
    this.ruleNameRef && this.ruleNameRef.focus();
    if (this.step === 1) {
      this.initStep();
    }
  }

  @Emit('cancel')
  public handleStepCancel() {}
  @Emit('step-change')
  public handleStepChange(step: number) {
    return step;
  }
  @Emit('update-reload')
  public handleUpdateReload({ step, needReload }: { step: number, needReload?: boolean }) {
    return { step, needReload };
  }
  @Emit('update-loaded')
  public handleUpdateStepLoaded({ step, loaded }: { step: number, loaded?: boolean }) {
    return { step, loaded };
  }

  public handleTargetChange(value: ISelectorValue) {
    const { host_list, node_list } = value;
    let type = '';
    if (node_list?.length) {
      type = 'TOPO';
    }
    if (host_list?.length) {
      type = 'INSTANCE';
    }
    if (!type) return;
    // 转换为旧版本的策略所需数据格式。 带meta属性的数据为新IP-selector
    PluginStore.setStateOfStrategy([
      {
        key: 'scope',
        value: {
          object_type: 'HOST',
          node_type: type,
          nodes: toStrategyNode(type === 'TOPO' ? node_list : host_list, type as INodeType),
        },
      },
    ]);
    this.stepChanged = true;
    // 需要重置 当前步骤为未作改动
    this.handleUpdateReload({ step: this.step + 1, needReload: true });
  }

  public initStep() {
    this.stepChanged = false;
    this.handleUpdateStepLoaded({ step: this.step, loaded: true });
  }
  public async beforeStepLeave() {
    const { stepChanged, formData: { policyName } } = this;
    PluginStore.setStateOfStrategy({ key: 'name', value: policyName });
    this.stepChanged = false;
    return Promise.resolve(!this.targetPreview.nodes.length || stepChanged);
  }
  public async handleNext() {
    const res = await this.computedForm.validate().catch(() => false);
    // if (!this.targetPreview.nodes.length) {
    //   this.$bkMessage({
    //     theme: 'error',
    //     message: this.$t('部署目标必须选择')
    //   })
    //   return
    // }
    if (res) {
      this.handleStepChange(this.step + 1);
    }
  }
}
</script>
<style lang="postcss" scoped>
>>> .bk-dialog {
  &-tool {
    min-height: 0;
  }
  &-body {
    padding: 0;
    height: 680px;
  }
}
>>> .rule-name .bk-form-content {
  width: 480px;
}
.selector-form {
  padding-right: 30px;
}
.ip-selector {
  /* stylelint-disable-next-line declaration-no-important */
  height: calc(100vh - 300px) !important;
}
</style>
