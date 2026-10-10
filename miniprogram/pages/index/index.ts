// index.ts
import { getSnapshot, postSnapshot, executeItem } from '../../api/broadcast'
import { getSummary } from '../../api/environment'
import { autoLocate, manualInputLocation } from '../../utils/location'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import { getTtsAdapter } from '../../utils/voice'
import type { PromptItem } from '../../types/api'

Page({
  data: {
    userAvatar: '/images/logo.jpg',
    currentCrop: '水稻（分蘖期）',
    farmerName: '张叔',
    locationName: '点击设置地点',
    // 提示快照
    tipItems: [] as PromptItem[],
    generatedLabel: '',
    // 跨日弱化：today 可勾选，其余只读展示
    isToday: true,
    // 环境一行状态
    envLine: '环境数据暂不可用',
    updating: false,
  },

  onLoad() {
    if (!ensureAuth()) return;
    this.loadUserInfo();
    this.loadLocation();
    this.loadSnapshot();
    this.loadEnvironment();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadUserInfo();
    this.loadLocation();
    this.loadSnapshot();
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

  /** 读取当日提示快照 */
  loadSnapshot() {
    getSnapshot()
      .then((res) => {
        this.applySnapshot(res.snapshot);
      })
      .catch(() => {
        // 离线：保留当前展示
      });
  },

  /** 更新提示：重新生成当日快照 */
  onUpdateTips() {
    if (this.data.updating) return;
    this.setData({ updating: true });
    postSnapshot()
      .then((res) => {
        this.applySnapshot(res.snapshot);
        if (res.updateNotice) {
          wx.showToast({ title: res.updateNotice, icon: 'none' });
        } else {
          wx.showToast({ title: '已更新', icon: 'success' });
        }
      })
      .catch((err) => {
        wx.showToast({ title: (err && err.message) || '更新失败', icon: 'none' });
      })
      .finally(() => {
        this.setData({ updating: false });
      });
  },

  applySnapshot(snapshot: any) {
    if (!snapshot) {
      this.setData({ tipItems: [], generatedLabel: '', isToday: false });
      return;
    }
    this.setData({
      tipItems: snapshot.items || [],
      generatedLabel: snapshot.generatedLabel || '',
      isToday: snapshot.displayStatus === 'today',
    });
  },

  /** 勾选 / 取消某条提示的执行状态（跨日快照不可操作） */
  onToggleExecuted(e: WechatMiniprogram.BaseEvent) {
    if (!this.data.isToday) return;
    const id = e.currentTarget.dataset.id as string;
    const item = this.data.tipItems.find((t) => t.itemId === id);
    if (!item) return;
    const next = !item.executed;

    // 乐观更新本地
    const tipItems = this.data.tipItems.map((t) =>
      t.itemId === id ? { ...t, executed: next } : t,
    );
    this.setData({ tipItems });

    executeItem(id, next).catch(() => {
      // 失败回滚
      const rollback = this.data.tipItems.map((t) =>
        t.itemId === id ? { ...t, executed: !next } : t,
      );
      this.setData({ tipItems: rollback });
      wx.showToast({ title: '操作失败，请重试', icon: 'none' });
    });
  },

  /** 核心动作一：我要提问 */
  onAsk() {
    wx.switchTab({ url: '/pages/ai/chat' });
  },

  /** 朗读一条提示：再点一次停止；朗读不改变提示的任何状态（无副作用） */
  onReadTip(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id as string;
    const item = this.data.tipItems.find((t) => t.itemId === id);
    if (!item) return;
    const text = [item.callout, item.fact, item.impact, item.action].filter((s) => s).join('。');
    const tts = getTtsAdapter();
    if (tts.isSpeaking()) {
      tts.stop();
      return;
    }
    tts.speak(text).catch(() => {
      wx.showToast({ title: '现在读不出来，稍后再试', icon: 'none' });
    });
  },

  /** 加载环境一行状态 */
  loadEnvironment() {
    getSummary()
      .then((d) => {
        this.setData({
          envLine: `温度 ${d.temperature}°C · 湿度 ${d.humidity}% · 土壤 ${d.soilStatus}`,
        });
      })
      .catch(() => {
        this.setData({ envLine: '环境数据暂不可用' });
      });
  },

  goProfile() {
    wx.navigateTo({ url: '/pages/profile/farmer-profile' });
  },
});
