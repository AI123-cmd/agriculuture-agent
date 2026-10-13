// pages/history/list.ts
import { history, removeHistory } from '../../api/ai'
import { ensureAuth } from '../../utils/guard'
import type { HistoryItem } from '../../types/api'

Page({
  data: {
    conversations: [] as HistoryItem[],
  },

  onLoad() {
    this.loadConversations();
  },

  onShow() {
    if (!ensureAuth()) return;
    this.loadConversations();
    const tabBar = (this as any).getTabBar();
    if (tabBar) tabBar.setData({ selected: 2 });
  },

  /** 会话列表以服务端为唯一来源（后端不再返回完整消息） */
  loadConversations() {
    history(1, 100)
      .then((res) => {
        const list = (res.list || []).slice().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
        this.setData({ conversations: list });
      })
      .catch(() => {
        // 离线：保留当前展示
      });
  },

  startNewChat() {
    // 同首页"我要提问"：置位后切到问答页，由问答页 onShow 消费并清空上一轮会话。
    getApp<IAppOption>().globalData.pendingNewChat = true;
    wx.switchTab({ url: '/pages/ai/chat' });
  },

  deleteConv(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id as string;
    wx.showModal({
      title: '确认删除',
      content: '确定要删除这个对话吗？',
      success: (res) => {
        if (!res.confirm) return;
        removeHistory(id)
          .then(() => {
            this.setData({
              conversations: this.data.conversations.filter((c) => c.id !== id),
            });
            wx.showToast({ title: '已删除', icon: 'success' });
          })
          .catch((err) => wx.showToast({ title: (err && err.message) || '删除失败', icon: 'none' }));
      },
    });
  },
});
