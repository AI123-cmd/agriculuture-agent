// pages/env/env.ts
import { getBoard, getDetail } from '../../api/environment'
import { autoLocate, manualInputLocation } from '../../utils/location'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import type { BoardMetric, DeviceListItem, SourceBadge } from '../../types/api'

/** 后端 lastReportAt 为 ISO 时间串，转为"刚刚 / N分钟前 / N小时前 / N天前" */
function formatReportTime(iso: string | null): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const diffMin = Math.floor((Date.now() - t) / 60000);
  if (diffMin < 1) return '刚刚';
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}小时前`;
  return `${Math.floor(diffHour / 24)}天前`;
}

/** 后端 DeviceListItem → 页面展示视图（状态字符串枚举转中文与样式类） */
function toDeviceView(item: DeviceListItem) {
  const state = item.onlineState;
  const stateLabel = state === 'online' ? '在线' : state === 'fault' ? '故障' : '离线';
  const dotClass = state === 'online' ? 'online' : state === 'fault' ? 'fault' : 'offline';
  const stateClass = state === 'online' ? 'state-online' : state === 'fault' ? 'state-fault' : 'state-offline';
  const timeLabel = state === 'online'
    ? (formatReportTime(item.lastReportAt) || '暂无上报')
    : (item.silenceNote || formatReportTime(item.lastReportAt) || '暂无上报');
  return {
    id: item.id,
    name: item.deviceName || item.deviceSn || '未命名设备',
    dotClass,
    stateClass,
    stateLabel,
    timeLabel,
  };
}

/** 后端 BoardMetric → 看板行视图：quality 非 ok 时弱化展示，status 映射为中文标签 */
function toBoardRow(item: BoardMetric) {
  const statusLabel = item.status === 'high' ? '偏高' : item.status === 'low' ? '偏低' : item.status === 'normal' ? '正常' : '';
  const statusClass = item.status === 'high' ? 'st-high' : item.status === 'low' ? 'st-low' : item.status === 'normal' ? 'st-normal' : '';
  // quality：ok 可信 / stale 过期 / suspect 超量程；非 ok 时数值仍给但弱化
  const weak = item.quality !== 'ok';
  return {
    metric: item.metric,
    label: item.label,
    value: item.value == null ? '—' : String(item.value),
    unit: item.unit || '',
    statusLabel,
    statusClass,
    weak,
  };
}

/** 来源标注 → 展示文案（freshnessNote 单独一行） */
function toSourceView(badge: SourceBadge | null | undefined) {
  if (!badge) return { label: '', note: '' };
  return {
    label: badge.label || '',
    note: badge.freshnessNote || (badge.regionHitLevel ? `当前按 ${badge.regionHitLevel} 通用数据` : ''),
  };
}

Page({
  data: {
    locationName: '点击设置地点',
    lastUpdateAt: '',
    loading: false,

    // 仪表盘核心指标（默认兜底，服务端数据到达后覆盖）
    metrics: [
      { id: 'temp', label: '气温', value: '26.5', unit: '°C' },
      { id: 'humidity', label: '湿度', value: '78', unit: '%' },
      { id: 'soil', label: '土壤湿度', value: '23', unit: '%' },
      { id: 'light', label: '光照', value: '58k', unit: 'lux' },
    ],

    // 来源标注（来自看板）
    sourceLabel: '',
    sourceNote: '',

    // 看板指标行
    boardMetrics: [] as Array<{
      metric: string
      label: string
      value: string
      unit: string
      statusLabel: string
      statusClass: string
      weak: boolean
    }>,

    // 空气环境
    airData: [
      { label: '空气温度', value: '26.5°C' },
      { label: '空气湿度', value: '78%' },
      { label: '风速', value: '2.3 m/s' },
      { label: '风向', value: '东南风' },
    ],

    // 土壤数据
    soilData: [
      { label: '土壤温度', value: '25.0°C' },
      { label: '土壤含水率', value: '23%' },
      { label: '土壤pH值', value: '6.6' },
      { label: '电导率(EC)', value: '1.2 mS/cm' },
    ],

    // 光照与气象
    weatherData: [
      { label: '光照强度', value: '58000 lux' },
      { label: '日照时长', value: '7.2 h' },
      { label: '紫外线指数', value: '中等 (UV 4)' },
      { label: '降雨量', value: '0 mm' },
    ],

    // 设备列表
    devices: [] as Array<{
      id: string
      name: string
      dotClass: string
      stateClass: string
      stateLabel: string
      timeLabel: string
    }>,
  },

  onLoad() {
    this.loadLocation();
    this.loadEnvironment();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadLocation();
    this.loadEnvironment();
  },

  goBack() {
    wx.navigateBack({ delta: 1 });
  },

  /** 从本地缓存读取地点 */
  loadLocation() {
    const saved = wx.getStorageSync(KEY.FARM_LOCATION);
    if (saved) {
      this.setData({ locationName: saved });
    }
  },

  /** 加载环境数据：看板（指标+来源+设备）为主，详情补充各分组明细 */
  loadEnvironment() {
    return Promise.all([
      getBoard().catch(() => null),
      getDetail().catch(() => null),
    ]).then(([board, detail]) => {
      const patch: any = {};

      if (board) {
        const src = toSourceView(board.sourceBadge);
        patch.boardMetrics = (board.metrics || []).map(toBoardRow);
        patch.sourceLabel = src.label;
        patch.sourceNote = src.note;
        patch.devices = (board.devices || []).map(toDeviceView);
        if (board.lastUpdateAt) {
          patch.lastUpdateAt = formatReportTime(board.lastUpdateAt);
        }
      }

      if (detail) {
        if (detail.metrics && detail.metrics.length > 0) patch.metrics = detail.metrics;
        if (detail.airData) patch.airData = detail.airData;
        if (detail.soilData) patch.soilData = detail.soilData;
        if (detail.weatherData) patch.weatherData = detail.weatherData;
        if (!board && detail.devices) patch.devices = (detail.devices || []).map(toDeviceView);
        if (!board && detail.lastUpdateAt) patch.lastUpdateAt = detail.lastUpdateAt;
        if (!board) {
          const src = toSourceView(detail.sourceBadge);
          patch.sourceLabel = src.label;
          patch.sourceNote = src.note;
        }
      }

      if (Object.keys(patch).length > 0) this.setData(patch);
    });
  },

  refreshData() {
    if (this.data.loading) return;
    this.setData({ loading: true });
    this.loadEnvironment().then(() => {
      this.setData({ loading: false });
      wx.showToast({ title: '已刷新', icon: 'success' });
    });
  },

  /** 直接在本页设置地点（弹出和首页相同的选择弹窗，不回跳） */
  goToLocation() {
    wx.showActionSheet({
      itemList: ['自动定位', '手动输入地点'],
      success: (res) => {
        if (res.tapIndex === 0) {
          autoLocate()
            .then((r) => {
              this.setData({ locationName: r.locationName });
              wx.showToast({ title: '定位成功', icon: 'success' });
            })
            .catch(() => {
              wx.showToast({ title: '定位失败，请手动设置', icon: 'none' });
              this.manualInput();
            });
        } else {
          this.manualInput();
        }
      },
    });
  },

  manualInput() {
    manualInputLocation()
      .then((r) => {
        this.setData({ locationName: r.locationName });
        wx.showToast({ title: '已设置', icon: 'success' });
      })
      .catch(() => {});
  },
});