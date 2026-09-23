// index.ts
import { getToday, markDone } from '../../api/broadcast'
import { getSummary } from '../../api/environment'
import { autoLocate, manualInputLocation } from '../../utils/location'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import type { BroadcastItem } from '../../types/api'

Page({
  data: {
    userAvatar: '/images/logo.jpg',
    currentCrop: '水稻（分蘖期）',
    farmerName: '张叔',
    locationName: '点击设置地点',
    broadcasts: [] as BroadcastItem[],
    functions: [
      { id: 'ai', label: 'AI咨询' },
      { id: 'camera', label: '拍照识别' },
      { id: 'env', label: '查看环境' },
      { id: 'manage', label: '农事管理' },
    ],
    envData: [
      { id: 1, value: '--', label: '温度' },
      { id: 2, value: '--', label: '湿度' },
      { id: 3, value: '--', label: '土壤' },
      { id: 4, value: '--', label: '光照' },
    ],
  },

  onLoad() {
    if (!ensureAuth()) return;
    this.loadUserInfo();
    this.loadLocation();
    this.loadBroadcasts();
    this.loadEnvironment();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadUserInfo();
    this.loadLocation();
    this.loadBroadcasts();
    this.loadEnvironment();
    const tabBar = (this as any).getTabBar();
    if (tabBar) tabBar.setData({ selected: 0 });
  },

  loadUserInfo() {
    const userInfo = wx.getStorageSync(KEY.USER_INFO);
    const farmerName = wx.getStorageSync(KEY.FARMER_NAME);
    const farmerCrops = wx.getStorageSync(KEY.FARMER_CROPS) || [];
    const patch: any = {};
    if (userInfo && userInfo.avatarUrl) {
      patch.userAvatar = userInfo.avatarUrl;
    }
    // 优先显示农户档案中的真实姓名，没有则用微信昵称
    patch.farmerName = farmerName || (userInfo && userInfo.nickName) || '未设置';
    // 同步农户档案中的作物信息到首页显示
    if (farmerCrops.length > 0) {
      const first = farmerCrops[0];
      const stage = first.growthStage ? `（${first.growthStage}）` : '';
      patch.currentCrop = `${first.cropName}${stage}`;
    }
    this.setData(patch);
  },

  onFuncTap(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id as string;
    switch (id) {
      case 'ai':
        wx.switchTab({ url: '/pages/ai/chat' });
        break;
      case 'camera':
        wx.chooseMedia({
          count: 1,
          mediaType: ['image'],
          sourceType: ['camera', 'album'],
          success: () => {
            // switchTab 不支持 URL 参数，用 storage 传递拍照模式标记
            wx.setStorageSync(KEY.PENDING_CHAT_MODE, 'camera');
            wx.switchTab({ url: '/pages/ai/chat' });
          },
        });
        break;
      case 'env':
        wx.navigateTo({ url: '/pages/env/env' });
        break;
      case 'manage':
        wx.navigateTo({ url: '/pages/profile/farmer-profile' });
        break;
      default:
        wx.showToast({ title: '功能开发中', icon: 'none' });
    }
  },

  goProfile() {
    wx.navigateTo({ url: '/pages/profile/farmer-profile' });
  },

  /** 加载本地缓存的地点 */
  loadLocation() {
    const saved = wx.getStorageSync(KEY.FARM_LOCATION);
    if (saved) {
      this.setData({ locationName: saved });
    }
  },

  /** 选择地点：自动定位 或 手动输入 */
  chooseLocation() {
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

  /** 加载今日农事播报 */
  loadBroadcasts() {
    getToday()
      .then((data) => {
        this.setData({ broadcasts: data.items || [] });
      })
      .catch(() => {
        // 离线：保留本地缓存/默认
      });
  },

  /** 加载环境摘要 */
  loadEnvironment() {
    getSummary()
      .then((d) => {
        this.setData({
          envData: [
            { id: 1, value: `${d.temperature}°C`, label: '温度' },
            { id: 2, value: `${d.humidity}%`, label: '湿度' },
            { id: 3, value: d.soilStatus, label: '土壤' },
            { id: 4, value: d.lightStatus, label: '光照' },
          ],
        });
      })
      .catch(() => {
        // 离线：保留默认展示
      });
  },

  /** 标记播报项完成 */
  onBroadcastDone(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id as string;
    const item = this.data.broadcasts.find((b) => b.id === id);
    if (!item || item.done) return;

    // 乐观更新本地
    const broadcasts = this.data.broadcasts.map((b) => (b.id === id ? { ...b, done: true } : b));
    this.setData({ broadcasts });

    markDone(id).catch(() => {});
  },
});