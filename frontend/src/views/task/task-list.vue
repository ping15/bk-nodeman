<template>
  <div class="task-list" v-test="'history'">
    <p class="task-list-title">{{ $t('nav_任务历史') }}</p>
    <TaskListFilter
      :filter-data="filterData"
      :search-select-value="searchSelectValue"
      :date-time-range="date"
      :hide-auto-deploy="hideAutoDeploy"
      @search-change="handleSearchChange"
      @deploy-change="handleDeployChange"
      @picker-change="handlePickerChange"
      @paste="(param) => handlePaste(param)">
    </TaskListFilter>
    <TaskListTable
      class="mt15"
      ref="taskListTable"
      :loading="loading"
      :head-filter="filterData"
      :search-select-value="searchSelectValue"
      :filter-data="filterData"
      :table-list="tableList"
      :pagination="pagination"
      :highlight="highlightIds"
      :hide-auto-deploy="hideAutoDeploy"
      @filter-confirm="handleFilterHeaderChange"
      @filter-reset="handleFilterHeaderChange"
      @pagination-change="handlePaginationChange"
      @sort-change="handleSortHandle"
      @empty-clear="searchClear"
      @empty-refresh="getHistortListDebounce">
    </TaskListTable>
  </div>
</template>
<script lang="ts">
import { Component, Mixins, Watch } from 'vue-property-decorator';
import TaskListFilter from './task-list-filter.vue';
import TaskListTable from './task-list-table.vue';
import PollMixin from '@/common/poll-mixin';
import { IPagination, ISearchChild, ISearchItem } from '@/types';
import { ITaskParams, IHistory } from '@/types/task/task';
import { debounce, filterTimeFormat, searchSelectPaste } from '@/common/util';
import HeaderFilterMixins from '@/components/common/header-filter-mixins';
import { MainStore, TaskStore } from '@/store';
import { Route } from 'vue-router';

Component.registerHooks([
  'beforeRouteLeave',
]);

@Component({
  name: 'taskList',
  components: {
    TaskListFilter,
    TaskListTable,
  },
})
export default class TaskList extends Mixins(PollMixin, HeaderFilterMixins)<Dictionary> {
  private loading = true;
  private hideAutoDeploy = true;
  private storageKey = 'task_filter_autoDeploy';
  private date: Date[] = [];
  private dateType: 'date' | Dictionary = {
    text: window.i18n.t('近30天'),
    value() {
      const end = new Date();
      const start = new Date();
      start.setTime(start.getTime() - (3600 * 1000 * 24 * 30));
      return [start, end];
    },
  };
  private pagination: IPagination = {
    limit: 50,
    current: 1,
    count: 0,
    limitList: [50, 100, 200],
  };
  private tableList: IHistory[] = [];
  private sortData = { head: '', sortType: '' }; // 排序参数
  public filterData: ISearchItem[] = [];
  public searchSelectValue: ISearchItem[] = [];
  private getHistortListDebounce!: Function; // 搜索防抖
  private highlightIds: number[] = [];
  private pollStatus = ['running', 'pending']; // 需要轮询的状态

  private get selectedBiz() {
    return MainStore.selectedBiz;
  }

  private created() {
    this.initPage();
  }

  private mounted() {
    this.getHistortListDebounce = debounce(300, this.getHistortList);
  }
  // 进入详情页才缓存界面
  private beforeRouteLeave(to: Route, from: Route, next: () => void) {
    if (to.name === 'taskDetail') {
      MainStore.addCachedViews(from);
    } else {
      MainStore.deleteCachedViews(from);
    }
    next();
  }

  private initPage() {
    this.hideAutoDeploy = localStorage.getItem(this.storageKey) === 'true';
    const end = new Date();
    const start = new Date();
    start.setTime(start.getTime() - (3600 * 1000 * 24 * 30));
    this.date = [start, end];
    const { taskIds = [] } = this.$route.params;
    this.highlightIds = taskIds as number[];
    if (taskIds.length) {
      this.$bkMessage({
        theme: 'primary',
        message: this.$t('当前操作拆分为多个子任务请关注最终执行结果'),
      });
    }

    this.getHistortList();
    this.handleGetConditionData();
  }

  // 从第一页开始拉取数据
  @Watch('selectedBiz')
  public reGetHistoryList() {
    this.pagination.current = 1;
    this.getHistortListDebounce();
  }

  // 搜索框: search-select change
  public handleSearchChange(list: ISearchItem[]) {
    this.filterData.forEach((data: ISearchItem) => {
      const children = list.find((item: ISearchItem) => item.id === data.id);
      if (data.children) {
        data.children = data.children.map((child: ISearchChild) => {
          if (!children) {
            child.checked = false;
          } else {
            child.checked = children.values ? children.values.some(value => value.id === child.id) : false;
          }
          return child;
        });
      }
    });
    this.searchSelectValue = list;
    this.reGetHistoryList();
  }
  private handlePaste({ e, ele }: { e: { target: EventTarget; }, ele: any }) {
    searchSelectPaste({
      e,
      selectedValue: this.searchSelectValue,
      filterData: this.filterData,
      selectRef: ele,
      pushFn: this.handlePushValue,
      changeFn: this.handleValueChange,
      prop: 'inner_ip_list',
    });
    this.reGetHistoryList();
  }

  public handleDeployChange(value: boolean) {
    localStorage.setItem(this.storageKey, `${value}`);
    this.hideAutoDeploy = value;
    this.reGetHistoryList();
  }
  public handlePickerChange({ type, value }: { type: 'date' | 'dateType', value: Date[] | 'date' | Dictionary }) {
    console.log('handlePickerChange', type, value);
    if (type === 'date') {
      this.date = value as Date[];
    } else {
      this.dateType = value as 'date' | Dictionary;
      this.reGetHistoryList();
    }
  }

  // 表头筛选: table-head-filter change
  public handleFilterHeaderChange(param: { prop: string, list?: ISearchChild[] }) {
    if (param.list) {
      this.tableHeaderConfirm(param as { prop: string, list: ISearchChild[] });
    } else {
      this.tableHeaderReset(param);
    }
    this.reGetHistoryList();
  }

  // 排序: table-head-sort change
  public handleSortHandle({ prop, order }: { prop: string, order: string }) {
    this.sortData.head = prop;
    this.sortData.sortType = order === 'ascending' ? 'ASC' : 'DEC';
    this.getHistortListDebounce();
  }

  // 分页: page、limit change
  public handlePaginationChange({ type, value }: { type: 'limit' | 'current', value: number }) {
    this.pagination[type] = value;
    if (type === 'limit') {
      this.reGetHistoryList();
    } else {
      this.getHistortListDebounce();
    }
  }
  public async getHistortList() {
    this.loading = true;
    this.clearRuningQueue();

    const params = this.getCommonParams();
    const res = await TaskStore.requestHistoryTaskList(params);
    this.pagination.count = res.total;
    this.tableList = res.list;
    // 运行任务队列 pollMixin: runingQueue 非成功状态都可以再执行
    this.runingQueue = res.list.filter((item: IHistory) => item.status && this.pollStatus.includes(item.status))
      .map((item: IHistory) => item.id);
    this.loading = false;
  }
  // 清空任务队列
  public clearRuningQueue() {
    this.runingQueue.splice(0, this.runingQueue.length);
    this.timer && clearTimeout(this.timer);
    this.timer = null;
  }

  // 处理轮询的数据
  public async handlePollData() {
    const res = await TaskStore.requestHistoryTaskList({
      page: 1,
      pagesize: this.runingQueue.length,
      job_id: this.runingQueue,
    });
    res.list.forEach((item: IHistory) => {
      const index = this.tableList.findIndex((row: IHistory) => row.id === item.id);
      if (index > -1) {
        if (!this.pollStatus.includes(item.status)) {
          const i = this.runingQueue.findIndex((id: string | number) => id === item.id);
          this.runingQueue.splice(i, 1);
        }
        this.$set(this.tableList, index, item);
      }
    });
  }

  // 获取搜索条件
  public async handleGetConditionData() {
    const filterData = await TaskStore.getFilterList();
    this.filterData.splice(0, 0, ...filterData);
  }

  // 获取请求参数
  public getCommonParams(): ITaskParams {
    const params: any = {
      page: this.pagination.current,
      pagesize: this.pagination.limit,
      hide_auto_trigger_job: this.hideAutoDeploy,
    };
    // 近***时间段类型 不需要传入 end_time, 否则会出现最新任务拉取不到的bug
    if (this.dateType === 'date') {
      const [start, end] = this.date;
      params.start_time = start;
      params.end_time = end;
    } else {
      let [start] = this.dateType.value();
      if (this.dateType.text === this.$t('今天')) {
        start = `${filterTimeFormat(start, 'YYYY-mm-dd')} 00:00:00`;
      } else {
        start = filterTimeFormat(start);
      }
      params.start_time = start;
    }
    if (this.selectedBiz.length) {
      params.bk_biz_id = [...this.selectedBiz];
    }
    if (this.sortData.head && this.sortData.sortType) {
      params.sort = { head: this.sortData.head, sort_type: this.sortData.sortType };
    }
    this.searchSelectValue.forEach((item) => {
      if (item.values) {
        params[item.id] = item.values.map((value: ISearchChild) => value.id);
      }
    });
    return params;
  }
  public searchClear() {
    this.loading = true;
    this.hideAutoDeploy = false;
    this.handleSearchChange([]);
    this.handleSearchChange([]);
  }
}
</script>

<style lang="postcss" scoped>

.task-list {
  height: calc(100vh - 52px);
  padding: 20px 24px 0 24px;
  overflow: hidden;
  .task-list-title {
    margin: 0 0 4px 0;
    font-size: 16px;
    line-height: 21px;
    color: #313238;
  }
}

</style>
