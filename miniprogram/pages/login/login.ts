// login.ts
import { login } from '../../api/auth'
import { getProfile } from '../../api/farmer'
import { KEY } from '../../utils/constants'

Page({
  data: {
    agreed: false,
    logging: false, // 防止重复点击
  },

  toggleAgree() {
    this.setData({
      agreed: !this.data.agreed,
    });
  },

  onWxLogin() {
    if (this.data.logging) return; // 防止重复点击
    if (!this.data.agreed) {
      wx.showToast({
        title: '请先同意用户协议和隐私政策',
        icon: 'none',
        duration: 2000,
      });
      return;
    }
    this.setData({ logging: true });

    wx.getUserProfile({
      desc: '用于完善用户资料',
      success: (res) => {
        const { userInfo } = res;
        wx.setStorageSync(KEY.USER_INFO, userInfo);
        wx.setStorageSync(KEY.HAS_LOGIN, true);
        this.loginWithCode(userInfo);
      },
      fail: () => {
        this.setData({ logging: false });
        wx.showToast({
          title: '登录已取消',
          icon: 'none',
        });
      },
    });
  },

  /** 登录成功后路由判断：有农户档案 → 首页，无 → 强制建档 */
  routeAfterLogin() {
    // 服务端为权威来源，获取失败时回退本地档案判断
    getProfile()
      .then((res) => {
        if (res.hasProfile && res.profile) {
          wx.setStorageSync(KEY.FARMER_PROFILE, res.profile);
          wx.setStorageSync(KEY.FARMER_CROPS, res.profile.crops || []);
          wx.setStorageSync(KEY.FARMER_NAME, res.profile.realName);
          wx.reLaunch({ url: '/pages/index/index' });
        } else {
          wx.reLaunch({ url: '/pages/profile/farmer-profile' });
        }
      })
      .catch(() => {
        const farmerProfile = wx.getStorageSync(KEY.FARMER_PROFILE);
        if (farmerProfile && farmerProfile.realName) {
          wx.reLaunch({ url: '/pages/index/index' });
        } else {
          wx.reLaunch({ url: '/pages/profile/farmer-profile' });
        }
      });
  },

  loginWithCode(userInfo: any) {
    wx.login({
      success: (res) => {
        if (!res.code) {
          this.setData({ logging: false });
          wx.showToast({ title: '登录失败，请重试', icon: 'none' });
          return;
        }
        login({ code: res.code, userInfo })
          .then((data) => {
            wx.setStorageSync(KEY.TOKEN, data.token);
            wx.setStorageSync(KEY.USER_ID, data.userId);
            wx.setStorageSync(KEY.HAS_LOGIN, true);
            wx.showToast({
              title: '登录成功',
              icon: 'success',
              duration: 1500,
              complete: () => {
                setTimeout(() => {
                  this.routeAfterLogin();
                }, 1500);
              },
            });
          })
          .catch((err) => {
            this.setData({ logging: false });
            wx.showToast({ title: (err && err.message) || '登录失败，请重试', icon: 'none' });
          });
      },
      fail: () => {
        this.setData({ logging: false });
        wx.showToast({
          title: '登录失败，请重试',
          icon: 'none',
        });
      },
    });
  },

  openUserAgreement() {
    wx.showToast({
      title: '用户协议页面开发中',
      icon: 'none',
    });
  },

  openPrivacyPolicy() {
    wx.showToast({
      title: '隐私政策页面开发中',
      icon: 'none',
    });
  },
});