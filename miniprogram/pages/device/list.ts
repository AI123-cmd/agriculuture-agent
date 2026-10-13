// pages/device/list.ts
import { listDevices, unbindDevice } from '../../api/device'
import { ensureAuth } from '../../utils/guard'
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

/** 后端 DeviceListItem → 列表展示视图（状态字符串枚举转中文与样式类） */
function toDeviceView(item: DeviceListItem) {
  const state = item.onlineState;
  const stateLabel = state === 'online' ? '在线' : state === 'fault' ? '故障' : '离线';
  const stateClass = state === 'online' ? 'state-online' : state === 'fault' ? 'state-fault' : 'state-offline';
  const timeLabel = state === 'online'
    ? (formatReportTime(item.lastReportAt) || '暂无上报')
    : (item.silenceNote || formatReportTime(item.lastReportAt) || '暂无上报');
  return {
    id: item.id,
    name: item.deviceName || item.deviceSn || '未命名设备',
    sn: item.deviceSn,
    stateClass,
    stateLabel,
    timeLabel,
    mappedFields: item.mappedFields,
    ready: item.mappedFields > 0,
  };
}

Page({
  data: {
    loading: false,
    devices: [] as Array<{
      id: string
      name: string
      sn: string
      stateClass: string
      stateLabel: string
      timeLabel: string
      mappedFields: number
      ready: boolean
    }>,
  },

  onLoad() {
    this.loadDevices();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadDevices();
  },

  goBack() {
    wx.navigateBack({ delta: 1 });
  },

  loadDevices() {
    this.setData({ loading: true });
    return listDevices()
      .then((res) => {
        this.setData({ devices: (res.devices || []).map(toDeviceView) });
      })
      .catch(() => {
        // 离线：保留当前展示
      })
      .finally(() => {
        this.setData({ loading: false });
      });
  },

  /** 新增设备：进入三步接入向导 */
  goBind() {
    wx.navigateTo({ url: '/pages/device/bind' });
  },

  /** 接入未完成（无字段映射）时继续向导的第二步 */
  continueSetup(e: WechatMiniprogram.BaseEvent) {
    const { id, sn } = e.currentTarget.dataset;
    wx.navigateTo({
      url: `/pages/device/bind?deviceId=${id}&deviceSn=${encodeURIComponent(sn || '')}`,
    });
  },

  confirmUnbind(e: WechatMiniprogram.BaseEvent) {
    const { id, name } = e.currentTarget.dataset;
    wx.showModal({
      title: '解绑设备',
      content: `确定解绑「${name}」吗？历史读数与映射会保留。`,
      confirmText: '解绑',
      confirmColor: '#d32f2f',
      success: (res) => {
        if (!res.confirm) return;
        unbindDevice(id)
          .then(() => {
            this.setData({ devices: this.data.devices.filter((d) => d.id !== id) });
            wx.showToast({ title: '已解绑', icon: 'success' });
          })
          .catch((err) => {
            wx.showToast({ title: (err && err.message) || '解绑失败', icon: 'none' });
          });
      },
    });
  },
});
