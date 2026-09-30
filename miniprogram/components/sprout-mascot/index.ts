// 小禾 IP 形象组件：纯 CSS 绘制，矢量清晰、零图片体积
// 设计含义：绿芽（泛指各类作物，不限水稻）+ 草帽（农户身份认同）
Component({
  properties: {
    // 整体边长（rpx），内部以 200rpx 基准等比缩放
    size: {
      type: Number,
      value: 120,
    },
  },

  data: {
    scale: 0.6,
  },

  observers: {
    size(s: number) {
      this.setData({ scale: s / 200 });
    },
  },

  lifetimes: {
    attached() {
      this.setData({ scale: this.data.size / 200 });
    },
  },
});
