// pages/ai/chat.ts
import { chat } from '../../api/ai'
import { ensureAuth } from '../../utils/guard'
import { KEY } from '../../utils/constants'
import { getTtsAdapter, getAsrAdapter } from '../../utils/voice'
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
    // 录音态：驱动按钮视觉反馈
    recording: false,
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

  /** 朗读一条 AI 消息（普通朗读按钮与转人工朗读按钮共用） */
  playVoice(e: WechatMiniprogram.BaseEvent) {
    const idx = e.currentTarget.dataset.index;
    const msg = this.data.messages[idx];
    if (!msg) return;
    const tts = getTtsAdapter();
    // 再点一次同一处则停止；朗读中切到其他条目由适配器内部先停后读
    if (tts.isSpeaking()) {
      tts.stop();
      return;
    }
    tts.speak(msg.content).catch(() => {
      wx.showToast({ title: '现在读不出来，稍后再试', icon: 'none' });
    });
  },

  /** 朗读转人工卡片（朗读面向农户的文案，不含内部字段） */
  playHandoff() {
    const handoff = this.data.handoff;
    if (!handoff) return;
    const parts: string[] = [handoff.summary];
    if (handoff.observationPoints.length > 0) {
      parts.push(`需要留意：${handoff.observationPoints.join('；')}`);
    }
    const tts = getTtsAdapter();
    if (tts.isSpeaking()) {
      tts.stop();
      return;
    }
    tts.speak(parts.join('。')).catch(() => {
      wx.showToast({ title: '现在读不出来，稍后再试', icon: 'none' });
    });
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

  /** 语音输入为主入口：按下开始录音，松手结束并识别（不要求点击两次） */
  onVoiceStart() {
    this.setData({ recording: true });
    getAsrAdapter()
      .recognize({ dialect: false })
      .then((res) => {
        // 识别结果先填入输入框等待确认，不直接发送
        this.setData({ inputValue: res.text, recording: false });
        if (!res.text) {
          wx.showToast({ title: '没听清，再说一次，或者打字告诉我', icon: 'none' });
        }
      })
      .catch((err) => {
        this.setData({ recording: false });
        wx.showToast({
          title: (err && err.message) || '需要允许使用麦克风才能语音提问，也可以直接打字',
          icon: 'none',
        });
      });
  },

  /** 松手结束录音 */
  onVoiceEnd() {
    if (!this.data.recording) return;
    getAsrAdapter().stop();
  },

  /** 录音中离开页面时取消本次识别 */
  onUnload() {
    getAsrAdapter().cancel();
    getTtsAdapter().stop();
  },

  /** 顶部返回按钮：返回首页 */
  goBack() {
    wx.switchTab({ url: '/pages/index/index' });
  },
});
