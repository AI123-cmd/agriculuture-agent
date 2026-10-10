// pages/ai/chat.ts
import { chat } from '../../api/ai'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import type { QuestionView, PrescriptionView, HandoffView } from '../../types/api'

interface Message {
  id: number;
  role: 'ai' | 'user';
  content: string;
  showVoice?: boolean;
  /** 转人工消息带独立朗读按钮 */
  speakable?: boolean;
}

Page({
  data: {
    messages: [] as Message[],
    inputValue: '',
    scrollTop: 0, // 改用 scroll-top 数值绑定
    scrollWithAnimation: true,
    sessionId: '',
    stageLabel: '',
    progressText: '',
    cropList: [] as string[],     // 档案中的作物列表
    currentCropName: '',          // 当前选中的作物名称
    // 结构化结果：questions / prescription / handoff 三者最多一个非空
    questions: [] as QuestionView[],
    prescription: null as PrescriptionView | null,
    handoff: null as HandoffView | null,
  },

  onLoad() {
    this.loadCropTags();
    this.initChat();
  },

  onShow() {
    if (!ensureAuth()) return;
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
        sessionId: '',
        questions: [],
        prescription: null,
        handoff: null,
        stageLabel: '',
        progressText: '',
        scrollWithAnimation: false,
      },
      () => {
        wx.nextTick(() => this.scrollToBottom(false));
      }
    );
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

    this.requestAIResponse({ message: text });
  },

  /** 作答追问：选项点选或填空 */
  onAnswerOption(e: WechatMiniprogram.BaseEvent) {
    const ds = e.currentTarget.dataset;
    const slotKey = ds.slot as string;
    const value = ds.value as string;
    const userMsg: Message = {
      id: Date.now(),
      role: 'user',
      content: value,
    };
    this.setData(
      {
        messages: [...this.data.messages, userMsg],
        questions: [],
        scrollWithAnimation: true,
      },
      () => wx.nextTick(() => this.scrollToBottom(true))
    );
    this.requestAIResponse({ answers: [{ slotKey, value, status: 'filled' }] });
  },

  /** 对“答不出”的处理：unknown 是合法答案 */
  onAnswerUnknown(e: WechatMiniprogram.BaseEvent) {
    const slotKey = e.currentTarget.dataset.slot as string;
    this.setData({ questions: [] });
    this.requestAIResponse({ answers: [{ slotKey, value: '', status: 'unknown' }] });
  },

  /** 调用后端图驱动问诊接口 */
  requestAIResponse(payload: { message?: string; answers?: { slotKey: string; value: string; status: 'filled' | 'unknown' }[] }) {
    wx.showLoading({ title: '思考中...' });

    chat({
      sessionId: this.data.sessionId || null,
      message: payload.message ?? null,
      processHint: this.data.currentCropName || null,
      answers: payload.answers ?? null,
    })
      .then((res) => {
        wx.hideLoading();
        const aiMsg: Message = {
          id: Date.now(),
          role: 'ai',
          content: res.reply || '（暂无回复）',
          showVoice: !!res.reply,
          speakable: !!res.handoff,
        };

        this.setData({
          messages: [...this.data.messages, aiMsg],
          sessionId: res.sessionId,
          stageLabel: res.stageLabel || '',
          progressText: res.progress ? `${res.progress.current}/${res.progress.total}` : '',
          questions: res.questions || [],
          prescription: res.prescription || null,
          handoff: res.handoff || null,
          scrollWithAnimation: true,
        }, () => {
          wx.nextTick(() => this.scrollToBottom(true));
        });
      })
      .catch((err) => {
        wx.hideLoading();
        // 网络异常：保留用户已发送消息，提示离线，不伪造 AI 回复
        wx.showToast({ title: (err && err.message) || '网络异常，请稍后重试', icon: 'none' });
      });
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
