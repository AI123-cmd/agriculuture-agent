// components/nav-bar/index.ts
Component({
  options: {
    multipleSlots: true,
  },

  properties: {
    // 页面标题
    title: {
      type: String,
      value: '话农点金',
    },
    // 标题颜色主题：white（深绿底白字）/ dark（浅色底深色字）
    theme: {
      type: String,
      value: 'white',
    },
    // 是否显示返回按钮
    showBack: {
      type: Boolean,
      value: false,
    },
    // 背景色：默认透明，由页面自己控制背景
    background: {
      type: String,
      value: 'transparent',
    },
  },

  data: {
    statusBarHeight: 0,
    navBarHeight: 44, // 默认导航栏高度（px）
    sideWidth: 88, // 左右两侧占位宽度，保证标题绝对居中
  },

  lifetimes: {
    attached() {
      const systemInfo = wx.getSystemInfoSync();
      const menuButtonInfo = wx.getMenuButtonBoundingClientRect();
      const statusBarHeight = systemInfo.statusBarHeight || 0;

      // 导航栏内容高度 = 胶囊按钮高度 + 上下间距
      const navBarHeight = menuButtonInfo.height + (menuButtonInfo.top - statusBarHeight) * 2;
      // 右侧胶囊占用宽度 = 屏幕宽 - 胶囊左边界，左右两侧同宽保证标题居中
      const sideWidth = systemInfo.windowWidth - menuButtonInfo.left;

      this.setData({
        statusBarHeight,
        navBarHeight,
        sideWidth,
      });
    },
  },

  methods: {
    onBack() {
      const pages = getCurrentPages();
      if (pages.length > 1) {
        wx.navigateBack();
      } else {
        // 没有上一页，回到首页
        wx.switchTab({ url: '/pages/index/index' });
      }
    },
  },
});
