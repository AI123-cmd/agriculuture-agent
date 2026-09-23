// pages/history/list.ts
import { history, removeHistory } from '../../api/ai'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'

interface Conversation {
  id: string;
  title: string;
  preview: string;
  updatedAt: number;
}

const STORAGE_KEY_CONV = KEY.CONVERSATION_LIST;

Page({
  data: {
    conversations: [] as Conversation[],
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

  /** 本地优先渲染，服务端覆盖（服务端为权威来源） */
  loadConversations() {
    const cached: any[] = wx.getStorageSync(STORAGE_KEY_CONV) || [];
    const cachedConv: Conversation[] = cached.map((c) => ({
      id: c.id,
      title: c.title,
      preview: c.preview,
      updatedAt: c.updatedAt,
    }));
    cachedConv.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    this.setData({ conversations: cachedConv });

    history(1, 100)
      .then((res) => {
        const list = (res.list || []).map((it) => ({
          id: it.id,
          title: it.title,
          preview: it.preview,
          updatedAt: it.updatedAt,
          messages: (it.messages || []).map((m) => ({
            id: m.id,
            role: m.role,
            content: m.content,
          })),
        }));
        list.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));

        // 保存完整消息，供 chat 页恢复对话
        wx.setStorageSync(STORAGE_KEY_CONV, list);
        this.setData({
          conversations: list.map((c) => ({
            id: c.id,
            title: c.title,
            preview: c.preview,
            updatedAt: c.updatedAt,
          })),
        });
      })
      .catch(() => {
        // 离线：保留本地缓存
      });
  },

  startNewChat() {
    wx.switchTab({ url: '/pages/ai/chat' });
  },

  enterChat(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id;
    // switchTab 不支持 URL 参数，用 storage 传递 sessionId
    wx.setStorageSync(KEY.PENDING_SESSION_ID, id);
    wx.switchTab({ url: '/pages/ai/chat' });
  },

  deleteConv(e: WechatMiniprogram.BaseEvent) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: '确认删除',
      content: '确定要删除这个对话吗？',
      success: (res) => {
        if (!res.confirm) return;

        // 先本地移除，再通知服务端
        const list: any[] = wx.getStorageSync(STORAGE_KEY_CONV) || [];
        wx.setStorageSync(STORAGE_KEY_CONV, list.filter((c) => c.id !== id));
        this.setData({
          conversations: this.data.conversations.filter((c) => c.id !== id),
        });

        removeHistory(id)
          .then(() => wx.showToast({ title: '已删除', icon: 'success' }))
          .catch((err) => wx.showToast({ title: (err && err.message) || '删除失败', icon: 'none' }));
      },
    });
  },
});