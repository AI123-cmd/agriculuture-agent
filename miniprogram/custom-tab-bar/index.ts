Component({
  data: {
    selected: 0,
  },

  methods: {
    switchTab(e: WechatMiniprogram.BaseEvent) {
      const index = e.currentTarget.dataset.index as number;
      const urls = [
        '/pages/index/index',
        '/pages/ai/chat',
        '/pages/history/list',
        '/pages/mine/mine',
      ];
      wx.switchTab({ url: urls[index] });
    },
  },
});
