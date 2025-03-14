<template>
  <section class="task-detail-table" v-bkloading="{ isLoading: loading }" v-test="'detailTable'">
    <bk-table
      row-key="instanceId"
      :data="tableList"
      :pagination="pagination"
      :limit-list="pagination.limitList"
      :class="`${ fontSize }`"
      :max-height="tableHeight"
      @page-change="pageChange"
      @page-limit-change="limitChange"
      @select="handleSelect"
      @select-all="handleSelect">
      <!-- <bk-table-column
        class-name="row-select"
        type="selection"
        width="40"
        :resizable="false"
        :reserve-selection="true"
        :selectable="getSelectAbled">
      </bk-table-column> -->
      <NmColumn
        min-width="140"
        label="IPv4"
        prop="innerIp"
        width="125">
        <template #default="{ row }">
          {{ row.innerIp | filterEmpty }}
        </template>
      </NmColumn>
      <NmColumn
        label="IPv6"
        prop="innerIpv6"
        :width="innerIPv6Width"
        v-if="$DHCP">
        <template #default="{ row }">
          {{ row.innerIpv6 | filterEmpty }}
        </template>
      </NmColumn>
      <NmColumn :label="$t('管控区域')" prop="bkCloudName" min-width="90" :resizable="false" />
      <NmColumn min-width="100" :label="$t('业务')" prop="bkBizName" :resizable="false" />
      <NmColumn min-width="110" :label="$t('操作类型')" prop="opTypeDisplay" :resizable="false" />
      <NmColumn
        v-if="showTargetVersionColumn"
        min-width="110"
        :label="$t('目标版本')"
        :resizable="false">
        <template #default="{ row }">
          {{ row.targetVersion | filterEmpty }}
        </template>
      </NmColumn>
      <NmColumn v-else min-width="120" :label="$t('安装方式')" prop="isManual" :resizable="false">
        <template #default="{ row }">
          {{ installTypeCell(row.isManual) }}
        </template>
      </NmColumn>
      <NmColumn min-width="100" :label="$t('耗时')" prop="costTime" :resizable="false">
        <template #default="{ row }">
          {{ takesTimeFormat(row.costTime) }}
        </template>
      </NmColumn>
      <NmColumn
        prop="status"
        :label="$t('执行状态')"
        min-width="220"
        :render-header="renderFilterHeader">
        <template #default="{ row }">
          <div
            class="col-execution"
            v-if="'running' === row.status && showCommindBtn && commandStep.includes(row.step)">
            <span class="execut-mark execut-ignored"></span>
            <i18n tag="span" path="等待手动操作查看" class="execut-text">
              <bk-button text theme="primary" @click="handleRowView('viewCommind',row)">
                {{ $t('操作指引') }}
              </bk-button>
            </i18n>
          </div>
          <!-- is_running 区分已忽略且正在别的任务下执行的情况 -->
          <div class="col-execution" v-else>
            <loading-icon v-if="row.status === 'running'"></loading-icon>
            <span v-else :class="`execut-mark execut-${ row.status }`"></span>
            <span
              v-if="row.status === 'filtered' || row.status === 'ignored'"
              :class="[
                'execut-text',
                { 'has-icon': row.suppressedById || (row.exception && row.exception === 'is_running') }
              ]"
              :title="filteredTitle(row)"
              @click.stop="handleRowView(row.status, row)">
              {{ `${titleStatusMap[row.status]} ` }}
              ({{ row.statusDisplay | filterEmpty }}
              <i
                v-if="row.suppressedById || (row.exception && row.exception === 'is_running')"
                class="nodeman-icon nc-icon-audit filtered-icon">
              </i>)
            </span>
            <span class="execut-text" v-else>{{ row.statusDisplay | filterEmpty }}</span>
          </div>
        </template>
      </NmColumn>
      <NmColumn
        prop="colspaOpera"
        :width="145 + (fontSize === 'large' ? 20 : 0)"
        :label="$t('操作')"
        :resizable="false">
        <template #default="{ row }">
          <div>
            <bk-button
              v-test="'log'"
              class="mr10"
              text
              v-if="row.status !== 'filtered' && row.status !== 'ignored'"
              theme="primary"
              @click.stop="handleRowView('viewLog', row)">
              {{ $t('查看日志') }}
            </bk-button>
            <loading-icon v-if="row.loading"></loading-icon>
            <template v-else>
              <bk-button
                v-test="'singleRetry'"
                text
                v-if="row.status === 'failed'"
                theme="primary"
                @click="handleRowOperate('retry',[row])">
                {{ $t('重试') }}
              </bk-button>
              <bk-button
                v-test="'singleStop'"
                text
                v-if="row.status === 'running'"
                theme="primary"
                @click="handleRowOperate('stop',[row])">
                {{ $t('终止') }}
              </bk-button>
            </template>
          </div>
        </template>
      </NmColumn>

      <NmException
        slot="empty"
        :type="tableEmptyType"
        :delay="loading"
        @empty-clear="emptySearchClear"
        @empty-refresh="emptyRefresh" />
    </bk-table>
    <TaskDetailSlider
      :task-id="taskId"
      :slider="slider"
      :table-list="tableList"
      v-model="slider.show">
    </TaskDetailSlider>
  </section>
</template>

<script lang="ts">
import { Component, Prop, Emit, Mixins } from 'vue-property-decorator';
import { MainStore } from '@/store';
import { ITaskHost, IRow } from '@/types/task/task';
import { IPagination, ISearchItem } from '@/types';
import { isEmpty, takesTimeFormat } from '@/common/util';
import TaskDetailSlider from './task-detail-slider.vue';
import HeaderRenderMixin from '@/components/common/header-render-mixins';

@Component({
  name: 'task-detail-table',
  components: {
    TaskDetailSlider,
  },
})
export default class TaskDeatailTable extends Mixins(HeaderRenderMixin) {
  @Prop({ type: [String, Number], default: '' }) private readonly taskId!: string | number;
  @Prop({ type: String, default: '' }) private readonly status!: string;
  @Prop({ type: Object, default: () => ({
    limit: 50,
    current: 1,
    count: 0,
    limitList: [50, 100, 200],
  }),
  }) private readonly pagination!: IPagination;
  @Prop({ type: Array, default: () => ([]) }) private readonly tableList!: Array<ITaskHost>;
  @Prop({ type: Boolean, default: false }) private readonly loading!: boolean;
  @Prop({ type: Array, default: () => ([]) }) public readonly filterData!: ISearchItem[];
  @Prop({ type: Array, default: () => ([]) }) public readonly searchSelectValue!: ISearchItem[];
  @Prop({ type: Array, default: () => ([]) }) private readonly selected!: Array<IRow>;
  @Prop({ type: String, default: '' }) private readonly category!: string;
  @Prop({ type: String, default: '' }) private readonly operateHost!: string;
  @Prop({ type: Boolean, default: false }) private readonly showCommindBtn!: boolean;

  private commandLoading = false;
  private titleStatusMap: Dictionary = {
    running: window.i18n.t('正在执行'),
    failed: window.i18n.t('执行失败'),
    part_failed: window.i18n.t('部分失败'),
    success: window.i18n.t('执行成功'),
    stop: window.i18n.t('已终止'),
    pending: window.i18n.t('等待执行'),
    terminated: window.i18n.t('已终止'),
    filtered: window.i18n.t('已忽略'),
    ignored: window.i18n.t('已忽略'),
  };
  private slider: Dictionary = {
    show: false,
    isSingle: false,
    hostType: '',
    opType: '',
    row: {},
  };

  private get fontSize() {
    return MainStore.fontSize;
  }
  private get tableHeight() {
    return MainStore.windowHeight - 322;
  }
  private get commandStep() {
    return [
      // => proxy安装、agent安装、agent重装
      '安装', 'Install', this.$t('安装'),
      'Installation', this.$t('手动安装Guide'),
      // Agent卸载
      '卸载Agent', 'Uninstall Agent', this.$t('手动卸载Agent'),
      // Proxy卸载
      '卸载Proxy', 'Uninstall Proxy', this.$t('手动卸载Proxy'),
      // 卸载
      '卸载', 'Uninstall', 'Uninstallation', this.$t('手动卸载Guide'),
      // other
      this.$t('Proxy安装'),
    ];
  }
  private get showTargetVersionColumn() {
    return this.category === 'policy';
  }
  private get innerIPv6Width() {
    return this.tableList.some(row => !!row.innerIpv6) ? 270 : 80;
  }
  private get tableEmptyType() {
    return this.searchSelectValue.length ? 'search-empty' : 'empty';
  }

  @Emit('row-operate')
  public handleRowOperate(type: string, rowList: ITaskHost[]) {
    return { type, selected: rowList };
  }

  @Emit('pagination-change')
  public handlePaginationChange({ type, value }: { type: string, value: string | number }) {
    return { type, value };
  }

  @Emit('select-change')
  public handleSelect(selected: ITaskHost[]) {
    return selected;
  }

  public handleRowView(type: string, row: ITaskHost) {
    if (type === 'viewLog') {
      this.$router.push({
        name: 'taskLog',
        params: {
          taskId: this.taskId as string,
          instanceId: row.instanceId.toString(),
        },
        query: {
          page: String(this.pagination.current),
          pageSize: String(this.pagination.limit),
        },
      });
    } else if (type === 'viewCommind') {
      this.slider.isSingle = true;
      this.slider.row = row;
      this.slider.show = true;
      this.slider.opType = row.opType;
      this.slider.opTypeDisplay = row.opTypeDisplay;
      // if (row) {
      if (this.operateHost === 'Proxy') {
        this.slider.hostType = this.operateHost;
      } else {
        this.slider.hostType = row.bkCloudId === window.PROJECT_CONFIG.DEFAULT_CLOUD ? 'Agent' : 'Pagent';
      }
      // 目前已没有查看所有命令操作
      // } else {
      //   this.slider.hostType = this.operateHost === 'Proxy' ? this.operateHost : 'mixed'
      // }
    } else if (type === 'filterrd') { // 已忽略且正在运行的主机跳转
      if (row.jobId && row.exception && row.exception === 'is_running') {
        this.$router.push({
          name: 'taskLog',
          params: {
            taskId: `${row.jobId}`,
            hostInnerIp: row.ip,
          },
          query: {
            page: String(this.pagination.current),
            pageSize: String(this.pagination.limit),
          },
        });
      }
    } else if (type === 'ignored') {
      if (row.suppressedById) {
        const route = this.$router.resolve({
          name: 'pluginRule',
          query: { id: `${row.suppressedById}` },
        });
        window.open(route.href, '_blank');
      }
    }
  }
  // 分页
  public pageChange(page: number) {
    this.handlePaginationChange({ type: 'current', value: page || 1 });
  }
  // 分页条数
  public limitChange(limit: number) {
    this.handlePaginationChange({ type: 'limit', value: limit || 1 });
  }
  public filteredTitle(row: ITaskHost) {
    return `${this.titleStatusMap[row.status]} ${(row.statusDisplay || '').replace(/\s+/g, ' ')}`;
  }
  public getSelectAbled(row: ITaskHost) {
    return row.status !== 'filtered' && row.status !== 'pendding';
  }
  public installTypeCell(cell: boolean | undefined) {
    if (isEmpty(cell)) {
      return '--';
    }
    return cell ? this.$t('手动') : this.$t('远程');
  }
  public takesTimeFormat(date: number) {
    return takesTimeFormat(date);
  }
}
</script>

<style lang="postcss" scoped>
  @import "@/css/mixins/nodeman.css";

  .task-detail-table {
    .execut-text {
      &.has-icon {
        cursor: pointer;
      }
      &:hover .filtered-icon {
        color: #3a84ff;
      }
    }
    .primary,
    .running {
      color: #3a84ff;
    }
    .success {
      color: #2dcb56;
    }
    .warning,
    .filtered，
    .ignored {
      color: #ff9c01;
    }
    .failed,
    .stop {
      color: #ea3636;
    }
    .pending {
      color: #63656e;
    }
    .disabled {
      color: #c4c6cc;
      cursor: not-allowed;
    }
  }
  >>> .row-select .cell {
    padding-left: 24px;
    padding-right: 0;
  }
  >>> .row-ip .cell {
    padding-left: 24px;
  }
</style>
