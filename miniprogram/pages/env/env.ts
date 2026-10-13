// pages/env/env.ts
import { getDetail } from '../../api/environment'
import { autoLocate, manualInputLocation } from '../../utils/location'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import type { DeviceListItem } from '../../types/api'

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

  /** 加载环境详情（服务端为准，本地默认兜底） */
  loadEnvironment() {
    return getDetail()
      .then((d) => {
        this.setData({
          metrics: d.metrics || this.data.metrics,
          airData: d.airData || this.data.airData,
          soilData: d.soilData || this.data.soilData,
          weatherData: d.weatherData || this.data.weatherData,
          devices: (d.devices || []).map(toDeviceView),
          lastUpdateAt: d.lastUpdateAt || Date.now().toString(),
        });
      })
      .catch(() => {
        // 离线：保留默认/缓存展示
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