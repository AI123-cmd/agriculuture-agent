// pages/ai/chat.ts
import { chat } from '../../api/ai'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'

interface Message {
  id: number;
  role: 'ai' | 'user';
  content: string;
  showVoice?: boolean;
}

interface StoredConversation {
  id: string;
  title: string;
  preview: string;
  updatedAt: number;
  messages: Message[];
}

const STORAGE_KEY_CONV = KEY.CONVERSATION_LIST;

Page({
  data: {
    messages: [] as Message[],
    inputValue: '',
    scrollTop: 0, // 改用 scroll-top 数值绑定
    scrollWithAnimation: true,
    sessionId: '',
    cropList: [] as string[],     // 档案中的作物列表
    currentCropName: '',          // 当前选中的作物名称
  },

  onLoad(options: { sessionId?: string }) {
    this.loadCropTags();
    if (options && options.sessionId) {
      this.restoreConversation(options.sessionId);
    } else {
      this.initChat();
    }
  },

  onShow() {
    if (!ensureAuth()) return;
    // 从历史页跳转过来时，通过 storage 传递的 sessionId 恢复对话
    const pendingSessionId = wx.getStorageSync(KEY.PENDING_SESSION_ID);
    if (pendingSessionId) {
      wx.removeStorageSync(KEY.PENDING_SESSION_ID);
      this.restoreConversation(pendingSessionId);
    }
    const tabBar = (this as any).getTabBar();
    if (tabBar) tabBar.setData({ selected: 1 });
  },

  /** 从农户档案读取作物列表，无档案时用默认作物兜底 */
  loadCropTags() {
    const crops = wx.getStorageSync(KEY.FARMER_CROPS) || [];
    let cropNames: string[] = crops.map((c: { cropName: string }) => c.cropName).filter((n: string) => n);

    // 没有档案作物时，提供常用作物作为默认选择
    if (cropNames.length === 0) {
      cropNames = ['通用农业', '水稻', '小麦', '玉米', '辣椒', '烟草'];
    }

    this.setData({
      cropList: cropNames,
      currentCropName: cropNames[0],
    });
  },

  /** 弹出作物切换选择 */
  showCropSwitch() {
    const list = this.data.cropList;
    if (list.length <= 1) {
      wx.showToast({ title: '档案中只有一种作物', icon: 'none' });
      return;
    }
    wx.showActionSheet({
      itemList: list,
      success: (res) => {
        const picked = list[res.tapIndex];
        this.setData({ currentCropName: picked });
        wx.showToast({
          title: `已切换至${picked}模型`,
          icon: 'none',
          duration: 1500,
        });
      },
    });
  },

  /** 全新对话初始化 */
  initChat() {
    const welcome: Message = {
      id: Date.now(),
      role: 'ai',
      content: '你好，我是话农点金，请问有什么可以帮您？',
      showVoice: true,
    };
    this.setData(
      {
        messages: [welcome],
        sessionId: 'sess_' + Date.now(),
        scrollWithAnimation: false,
      },
      () => {
        wx.nextTick(() => this.scrollToBottom(false));
      }
    );
  },

  /** 从历史记录恢复对话 */
  restoreConversation(sessionId: string) {
    const list: StoredConversation[] = wx.getStorageSync(STORAGE_KEY_CONV) || [];
    const conv = list.find((c) => c.id === sessionId);
    if (conv && conv.messages && conv.messages.length > 0) {
      this.setData(
        {
          messages: conv.messages,
          sessionId: conv.id,
          scrollWithAnimation: false,
        },
        () => wx.nextTick(() => this.scrollToBottom(false))
      );
    } else {
      this.initChat();
    }
  },

  onInput(e: WechatMiniprogram.Input) {
    this.setData({ inputValue: e.detail.value });
  },

  sendMessage() {
    const text = this.data.inputValue.trim();
    if (!text) return;

    const userMsg: Message = {
      id: Date.now(),
      role: 'user',
      content: text,
    };

    const newMessages = [...this.data.messages, userMsg];
    this.setData(
      {
        messages: newMessages,
        inputValue: '',
        scrollWithAnimation: true,
      },
      () => {
        wx.nextTick(() => this.scrollToBottom(true));
      }
    );

    // 有用户消息了，就把这条对话写入/更新到历史列表
    this.saveConversationToStorage(newMessages, text);

    this.requestAIResponse(text);
  },

  /** 调用后端 AI 对话接口 */
  requestAIResponse(userText: string) {
    wx.showLoading({ title: '思考中...' });

    chat({
      message: userText,
      sessionId: this.data.sessionId,
      cropTag: this.data.currentCropName || null,
      imageUrl: null,
    })
      .then((res) => {
        wx.hideLoading();
        const aiMsg: Message = {
          id: Date.now(),
          role: 'ai',
          content: res.reply || '（暂无回复）',
          showVoice: true,
        };

        const newMessages = [...this.data.messages, aiMsg];
        this.setData(
          {
            messages: newMessages,
            scrollWithAnimation: true,
          },
          () => {
            wx.nextTick(() => this.scrollToBottom(true));
          }
        );

        // AI 回复完也同步更新历史记录的 preview
        this.saveConversationToStorage(newMessages, null, res.reply);
      })
      .catch((err) => {
        wx.hideLoading();
        // 网络异常：保留用户已发送消息，提示离线，不伪造 AI 回复
        wx.showToast({ title: (err && err.message) || '网络异常，请稍后重试', icon: 'none' });
      });
  },

  /**
   * 将当前对话保存到 storage，供历史页面读取（离线兜底）
   * @param messages 完整消息数组
   * @param userFirstText 用户消息，用于生成对话标题（首次有用户消息时传入）
   * @param lastAiText  AI最新回复，用于更新preview
   */
  saveConversationToStorage(messages: Message[], userFirstText: string | null, lastAiText?: string) {
    const list: StoredConversation[] = wx.getStorageSync(STORAGE_KEY_CONV) || [];
    const sid = this.data.sessionId;

    // 标题：第一条用户消息；否则"新对话"
    const firstUserMsg = messages.find((m) => m.role === 'user');
    const title = userFirstText || (firstUserMsg ? firstUserMsg.content.slice(0, 15) : '新对话');

    // 预览：最新一条消息的前30字
    const last = messages[messages.length - 1];
    const preview = (lastAiText || last ? last.content : '').slice(0, 30);

    const idx = list.findIndex((c) => c.id === sid);
    const record: StoredConversation = {
      id: sid,
      title,
      preview,
      updatedAt: Date.now(),
      messages,
    };

    if (idx >= 0) {
      list[idx] = record;
    } else {
      list.unshift(record);
    }

    wx.setStorageSync(STORAGE_KEY_CONV, list);
  },

  /**
   * 强制滚动到消息底部
   * 方案：不用 scroll-into-view，改用 scroll-top 的超大值
   */
  scrollToBottom(withAnimation: boolean) {
    this.setData({
      scrollWithAnimation: withAnimation,
      scrollTop: 0,
    });
    wx.nextTick(() => {
      const forceValue = 99999999 + Math.floor(Math.random() * 100000);
      this.setData({ scrollTop: forceValue });
      setTimeout(() => this.setData({ scrollTop: forceValue + 1 }), 50);
    });
  },

  playVoice(e: WechatMiniprogram.BaseEvent) {
    const idx = e.currentTarget.dataset.index;
    const _msg = this.data.messages[idx];
    wx.showToast({ title: '语音播放中...', icon: 'none', duration: 1000 });
  },

  showQuickQuestions() {
    const questions = [
      '叶子发黄是什么原因？',
      '如何防治稻飞虱？',
      '追肥用什么肥料好？',
      '什么时候浇水合适？',
      '病虫害怎么识别？',
    ];
    wx.showActionSheet({
      itemList: questions,
      success: (res) => {
        this.setData({ inputValue: questions[res.tapIndex] });
      },
    });
  },

  toggleVoice() {
    wx.showToast({ title: '语音功能开发中', icon: 'none' });
  },

  /** 顶部返回按钮：返回首页 */
  goBack() {
    wx.switchTab({ url: '/pages/index/index' });
  },
});