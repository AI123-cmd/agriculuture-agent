// pages/mine/mine.ts
import { uploadAvatar, cancelAccount } from '../../api/user'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import { BASE_URL } from '../../utils/config'

Page({
  data: {
    userAvatar: '/images/logo.jpg',
    nickName: '未登录',
    farmerName: '',
    showProfileEdit: false,
    pendingAvatar: '',
  },

  onLoad() {
    this.loadUserInfo();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadUserInfo();
    const tabBar = (this as any).getTabBar();
    if (tabBar) tabBar.setData({ selected: 3 });
  },

  loadUserInfo() {
    const userInfo = wx.getStorageSync(KEY.USER_INFO) || {};
    this.setData({
      userAvatar: userInfo.avatarUrl || '/images/logo.jpg',
      nickName: userInfo.nickName || '话农用户',
      farmerName: wx.getStorageSync(KEY.FARMER_NAME) || '',
    });
  },

  goProfile() {
    wx.navigateTo({ url: '/pages/profile/farmer-profile' });
  },

  goHistory() {
    wx.switchTab({ url: '/pages/history/list' });
  },

  showAbout() {
    wx.showModal({
      title: '关于话农点金',
      content: '话农点金是一款为农户提供AI农事咨询、种植管理、环境监测的智慧农业小程序。\n\n版本：v1.0.0',
      showCancel: false,
      confirmText: '知道了',
    });
  },

  showAgreement() {
    wx.showModal({
      title: '用户协议与隐私政策',
      content: '话农点金尊重并保护您的个人隐私。我们会收集您的微信头像、昵称及农户档案信息，仅用于提供个性化的农业咨询服务，不会向第三方共享。',
      showCancel: false,
      confirmText: '知道了',
    });
  },

  logout() {
    wx.showModal({
      title: '退出登录',
      content: '确定要退出登录吗？',
      confirmColor: '#d32f2f',
      success: (res) => {
        if (res.confirm) {
          // 清除所有本地存储的用户数据
          wx.removeStorageSync(KEY.TOKEN);
          wx.removeStorageSync(KEY.USER_ID);
          wx.removeStorageSync(KEY.USER_INFO);
          wx.removeStorageSync(KEY.HAS_LOGIN);
          wx.removeStorageSync(KEY.FARMER_NAME);
          wx.removeStorageSync(KEY.FARMER_CROPS);
          wx.removeStorageSync(KEY.FARMER_PROFILE);
          wx.removeStorageSync(KEY.FARM_LOCATION);
          wx.removeStorageSync(KEY.CONVERSATION_LIST);
          wx.removeStorageSync(KEY.PENDING_SESSION_ID);
          wx.removeStorageSync(KEY.PENDING_CHAT_MODE);

          wx.reLaunch({ url: '/pages/login/login' });
        }
      },
    });
  },

  cancelAccount() {
    wx.showModal({
      title: '注销账号',
      content: '注销后账号和所有数据将永久删除，无法恢复。确定要注销吗？',
      confirmText: '确认注销',
      confirmColor: '#d32f2f',
      success: (res) => {
        if (!res.confirm) return;

        // 二次确认
        wx.showModal({
          title: '最后确认',
          content: '此操作不可逆，您的农户档案、对话记录等所有数据将被清除。',
          confirmText: '我已知晓',
          confirmColor: '#d32f2f',
          success: (r) => {
            if (!r.confirm) return;

            cancelAccount()
              .then(() => this.clearAllAndLogout())
              .catch((err) => {
                wx.showToast({ title: (err && err.message) || '网络异常，请稍后重试', icon: 'none' });
              });
          },
        });
      },
    });
  },

  /** 清除所有本地数据并回到登录页 */
  clearAllAndLogout() {
    wx.clearStorageSync();
    wx.showToast({ title: '账号已注销', icon: 'none' });
    setTimeout(() => {
      wx.reLaunch({ url: '/pages/login/login' });
    }, 1000);
  },

  /* ---------- 头像修改 ---------- */

  showProfileActions() {
    // 现在只有修改头像一项，直接弹出底部编辑面板
    this.setData({
      showProfileEdit: true,
      pendingAvatar: '',
    });
  },

  hideProfileEdit() {
    this.setData({ showProfileEdit: false });
  },

  noop() {},

  onChooseAvatar(e: any) {
    const avatarUrl = e.detail.avatarUrl;
    if (!avatarUrl) return;
    // 加时间戳防缓存（同文件名但内容变更时，微信图片CDN会复用旧图）
    const stamped = avatarUrl + (avatarUrl.includes('?') ? '&' : '?') + 't=' + Date.now();
    this.setData({ pendingAvatar: stamped });
  },

  submitProfileEdit() {
    const { pendingAvatar } = this.data;
    if (!pendingAvatar) {
      wx.showToast({ title: '请先选择头像', icon: 'none' });
      return;
    }
    // chooseAvatar 返回临时路径，需走 wx.uploadFile 换取永久 URL；去掉时间戳参数
    const filePath = pendingAvatar.split('?')[0];

    uploadAvatar(filePath)
      .then((res) => {
        const raw = res.url || '';
        const permUrl = raw.startsWith('http') ? raw : BASE_URL + raw;
        const stamped = permUrl + (permUrl.includes('?') ? '&' : '?') + 't=' + Date.now();

        const userInfo = wx.getStorageSync(KEY.USER_INFO) || {};
        userInfo.avatarUrl = permUrl;
        wx.setStorageSync(KEY.USER_INFO, userInfo);

        this.setData({ userAvatar: stamped, showProfileEdit: false });
        wx.showToast({ title: '已保存', icon: 'success' });
      })
      .catch((err) => {
        // 网络异常：保留本地选择的头像，提示离线，不丢已选内容
        const userInfo = wx.getStorageSync(KEY.USER_INFO) || {};
        userInfo.avatarUrl = pendingAvatar;
        wx.setStorageSync(KEY.USER_INFO, userInfo);
        this.setData({ userAvatar: pendingAvatar, showProfileEdit: false });
        wx.showToast({ title: (err && err.message) || '上传失败', icon: 'none' });
      });
  },
});