// pages/ai/chat.ts
import { chat, conversationDetail } from '../../api/ai'
import { savePrescriptionToLedger } from '../../api/ledger'
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
    questions: [] as any[],
    /** 已作答的卡片数：多卡片需全部答完才提交 */
    qaAnswered: 0,
    /** 提交中标志，防止重复提交 */
    submitting: false,
    prescription: null as PrescriptionView | null,
    handoff: null as HandoffView | null,
    /** 处方卡是否已存入台账（驱动按钮文案与抑制重复点击） */
    ledgerSaved: false,
    // 录音态：驱动按钮视觉反馈
    recording: false,
  },

  /** 本轮卡片的作答暂存（slotKey -> 答案），不入 data，避免无谓渲染 */
  qaAnswers: {} as Record<string, { slotKey: string; value: string; status: 'filled' | 'unknown' }>,

  onLoad() {
    this.loadCropTags();
    this.initChat();
  },

  onShow() {
    if (!ensureAuth()) return;
    const tabBar = (this as any).getTabBar();
    if (tabBar) tabBar.setData({ selected: 1 });
    const app = getApp<IAppOption>();
    // 历史页点某条会话：置位会话 id，此处加载其历史消息。
    if (app.globalData.pendingOpenSession) {
      const sessionId = app.globalData.pendingOpenSession;
      app.globalData.pendingOpenSession = undefined;
      app.globalData.pendingNewChat = false;
      this.loadConversation(sessionId);
      return;
    }
    // 首页/历史页点"新对话"会置位该标志；此处消费并清空上一轮，
    // 只有"明确要开新对话"才重置，切 tab 回来仍保留进行中的会话。
    if (app.globalData.pendingNewChat) {
      app.globalData.pendingNewChat = false;
      this.initChat();
    }
  },

  /** 加载某条历史会话的消息与处方卡，并把 sessionId 接上，可继续追问 */
  loadConversation(sessionId: string) {
    wx.showLoading({ title: '加载中...' });
    conversationDetail(sessionId)
      .then((res) => {
        wx.hideLoading();
        const messages = (res.messages || []).map((m, i) => ({
          id: Date.now() + i,
          role: m.role,
          content: m.content,
          showVoice: m.role === 'ai',
        })) as Message[];
        this.setData(
          {
            messages,
            sessionId: res.sessionId,
            questions: [],
            qaAnswered: 0,
            submitting: false,
            // 还原该会话的处方卡与台账状态，使历史界面与实时界面一致
            prescription: res.prescription || null,
            handoff: null,
            ledgerSaved: !!res.ledgerSaved,
            stageLabel: '',
            progressText: '',
            scrollWithAnimation: false,
          },
          () => wx.nextTick(() => this.scrollToBottom(false))
        );
      })
      .catch((err) => {
        wx.hideLoading();
        wx.showToast({ title: (err && err.message) || '加载失败', icon: 'none' });
        this.initChat();
      });
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
        qaAnswered: 0,
        submitting: false,
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
    // 手动输入等同放弃当前卡片作答：清掉卡片与暂存，避免旧答案混入下一轮
    this.qaAnswers = {};
    this.setData(
      {
        messages: newMessages,
        inputValue: '',
        questions: [],
        qaAnswered: 0,
        scrollWithAnimation: true,
      },
      () => {
        wx.nextTick(() => this.scrollToBottom(true));
      }
    );

    this.requestAIResponse({ message: text });
  },

  /** 作答追问：先本地累积，全部卡片答完后一次性提交 */
  onAnswerOption(e: WechatMiniprogram.BaseEvent) {
    const ds = e.currentTarget.dataset;
    this.pickAnswer(ds.slot as string, ds.value as string, 'filled');
  },

  /** 对“答不出”的处理：unknown 是合法答案 */
  onAnswerUnknown(e: WechatMiniprogram.BaseEvent) {
    this.pickAnswer(e.currentTarget.dataset.slot as string, '', 'unknown');
  },

  /** 卡片内自由文本输入：仅更新该卡的暂存值，不触发提交 */
  onQaInput(e: WechatMiniprogram.Input) {
    const slotKey = e.currentTarget.dataset.slot as string;
    const value = e.detail.value;
    const updated = (this.data.questions as any[]).map((q) =>
      q.slotKey === slotKey ? { ...q, inputValue: value } : q
    );
    this.setData({ questions: updated });
  },

  /** 卡片内自由文本作答：把输入的文字作为该槽位的答案提交 */
  onQaTextConfirm(e: WechatMiniprogram.BaseEvent) {
    const slotKey = e.currentTarget.dataset.slot as string;
    const q = (this.data.questions as any[]).find((item) => item.slotKey === slotKey);
    const text = ((q && q.inputValue) || '').trim();
    if (!text) {
      wx.showToast({ title: '请先输入内容，或选“说不清”', icon: 'none' });
      return;
    }
    this.pickAnswer(slotKey, text, 'filled');
  },

  /**
   * 记录单张卡片的作答，并在全部答完时统一提交。
   *
   * 多张卡片同时出现时，若点一张就立即提交，后端只收到这一条答案，其余槽位被丢弃。
   * 后端 `apply_answers` 本就支持一次接收多条，故此处改为本地累积：每次点选只更新
   * 该卡片的选中态与进度，待全部答完一并提交，保证同轮问题一次问清。
   */
  pickAnswer(slotKey: string, value: string, status: 'filled' | 'unknown') {
    if (this.data.submitting) return;
    const questions = this.data.questions as any[];
    const picked = { ...this.qaAnswers, [slotKey]: { slotKey, value, status } };

    const updated = questions.map((q) =>
      q.slotKey === slotKey
        ? { ...q, picked: true, pickedOption: status === 'filled' ? value : '', pickedStatus: status }
        : q
    );
    const answered = updated.filter((q) => q.picked).length;
    this.qaAnswers = picked;
    this.setData({ questions: updated, qaAnswered: answered });

    if (answered >= updated.length) this.submitAnswers();
  },

  /** 全部卡片答完后统一提交：一次带上本轮所有答案 */
  submitAnswers() {
    if (this.data.submitting) return;
    const questions = this.data.questions as any[];
    const answers = questions.map((q) => this.qaAnswers[q.slotKey]).filter((a) => !!a);
    if (!answers.length) return;

    // 用一条汇总气泡回显本轮作答，避免逐张卡片各产一条消息打断阅读。
    // 文案带问题原文（而非槽位键），既便于农户回看，也作为落库的用户消息内容。
    const summary = questions
      .map((q) => {
        const a = this.qaAnswers[q.slotKey];
        if (!a) return '';
        const val = a.status === 'unknown' || !a.value ? '说不清' : a.value;
        return `${q.text}：${val}`;
      })
      .filter((s) => !!s)
      .join('；');
    this.setData({
      submitting: true,
      questions: [],
      qaAnswered: 0,
      messages: [...this.data.messages, { id: Date.now(), role: 'user', content: summary }],
      scrollWithAnimation: true,
    }, () => wx.nextTick(() => this.scrollToBottom(true)));
    this.qaAnswers = {};
    this.requestAIResponse({ message: summary, answers });
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
          questions: (res.questions || []).map((q: any) => ({ ...q, inputValue: '' })),
          qaAnswered: 0,
          submitting: false,
          prescription: res.prescription || null,
          handoff: res.handoff || null,
          ledgerSaved: false,
          scrollWithAnimation: true,
        }, () => {
          wx.nextTick(() => this.scrollToBottom(true));
        });
      })
      .catch((err) => {
        wx.hideLoading();
        // 网络异常：保留用户已发送消息，提示离线，不伪造 AI 回复。解除提交锁，
        // 让农户可直接重试，否则 submitting 会一直为真、卡片再也点不动。
        this.setData({ submitting: false });
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

  /** 处方卡存入农事台账（幂等；需后端下发 cardId，缺失时按钮不渲染） */
  onSaveLedger() {
    const cardId = this.data.prescription && this.data.prescription.cardId;
    if (!cardId || this.data.ledgerSaved) return;
    wx.showLoading({ title: '正在存入...' });
    savePrescriptionToLedger(cardId)
      .then(() => {
        wx.hideLoading();
        this.setData({ ledgerSaved: true });
        wx.showToast({ title: '已存入农事台账', icon: 'success' });
      })
      .catch((err) => {
        wx.hideLoading();
        wx.showToast({ title: (err && err.message) || '存入失败', icon: 'none' });
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
