import { getProfile, saveProfile } from '../../api/farmer'
import { KEY } from '../../utils/constants'

/** 流程类型选项（七选一，取后端 ProcessId 值）；'other' 表示自由输入作物名。 */
const PROCESS_OPTIONS = [
  { value: 'rice', label: '水稻' },
  { value: 'tea', label: '茶树' },
  { value: 'rapeseed', label: '油菜' },
  { value: 'chili', label: '辣椒' },
  { value: 'weed', label: '杂草防治' },
  { value: 'pig', label: '养猪' },
  { value: 'other', label: '其他作物' },
]

/** 流程类型 → 展示名（与后端各流程 YAML 的 display_name 对齐）。 */
const PROCESS_LABELS: Record<string, string> = {
  rice: '水稻',
  tea: '茶树',
  rapeseed: '油菜',
  chili: '辣椒',
  weed: '杂草防治',
  pig: '养猪',
  other: '其他作物',
}

/** 各流程的生长阶段词表（对齐各自 YAML 的 options）；缺省的流程不展示阶段选择。 */
const STAGE_BY_PROCESS: Record<string, string[]> = {
  rice: ['分蘖期', '孕穗期', '抽穗期', '灌浆期'],
  rapeseed: ['苗期', '蕾薹期', '初花期', '角果期'],
  chili: ['苗期', '开花坐果期', '膨果期', '采收期'],
  tea: ['春茶', '夏茶', '秋茶'],
  pig: ['哺乳仔猪', '保育猪', '育肥猪', '母猪'],
}

/** 作物名 → 流程类型的关键词表，用于回填与快捷添加时推断。顺序即匹配优先级。 */
const PROCESS_KEYWORDS: { pt: string; words: string[] }[] = [
  { pt: 'rice', words: ['水稻', '稻子', '稻谷', '早稻', '中稻', '晚稻', '粳稻', '籼稻'] },
  { pt: 'weed', words: ['杂草', '除草'] },
  { pt: 'tea', words: ['茶'] },
  { pt: 'rapeseed', words: ['油菜', '菜籽'] },
  { pt: 'chili', words: ['辣椒', '朝天椒', '线椒', '菜椒', '青椒'] },
  { pt: 'pig', words: ['养猪', '生猪', '猪'] },
]

/** 由作物名推断流程类型；都不命中则归入 'other'（自由输入）。 */
function inferProcess(name: string): string {
  const text = (name || '').trim()
  if (!text) return 'other'
  for (const group of PROCESS_KEYWORDS) {
    if (group.words.some((w) => text.includes(w))) return group.pt
  }
  return 'other'
}

function genId(): string {
  return `c${Date.now()}${Math.floor(Math.random() * 1000)}`
}

Page({
  data: {
    loading: false,

    // 表单数据
    form: {
      realName: '',
      gender: 0,           // 1男 2女
      age: '',
      phone: '',
      experienceYear: 0,
      experienceYearText: '',
      province: '',
      city: '',
      district: '',
      address: '',
      totalMu: '',
      cultivationType: 0,  // 1露地 2大棚 3混合
      waterSource: '',
      remark: '',
    },

    // 种植经验选项
    experienceOptions: [
      { label: '0 年（新手）', value: 0 },
      { label: '1 ~ 3 年（入门）', value: 1 },
      { label: '4 ~ 10 年（熟练）', value: 4 },
      { label: '10 年以上（资深）', value: 10 },
    ],

    // 灌溉水源选项
    waterOptions: ['自来水', '井水', '河水', '水库', '其他'],

    // 常用作物品类（快捷选择用）：仅保留已接入专家流程的作物
    cropList: ['水稻', '茶树', '油菜', '辣椒'],

    // 流程类型选项与阶段词表
    processOptions: PROCESS_OPTIONS,
    processLabels: PROCESS_LABELS,
    stageByProcess: STAGE_BY_PROCESS,

    // 添加作物的暂存态
    pendingProcessType: '',
    pendingProcessLabel: '',
    pendingCropName: '',

    // 已添加作物（含详细信息）
    selectedCrops: [] as any[],

    // 备注字数
    remarkLength: 0,
  },

  onLoad() {
    this.loadProfile();
  },

  /* ------------- 查询已有档案（回填用） ------------- */
  goBack() {
    wx.navigateBack({ delta: 1 });
  },

  loadProfile() {
    const token = wx.getStorageSync(KEY.TOKEN);
    if (!token) return;

    // 优先从本地缓存回填（秒开 + 离线兜底）
    const cached = wx.getStorageSync(KEY.FARMER_PROFILE);
    if (cached) {
      this.fillForm(cached);
    }

    // 服务端为权威来源，成功即覆盖本地缓存
    getProfile()
      .then((data) => {
        if (data && data.hasProfile && data.profile) {
          this.fillForm(data.profile);
          wx.setStorageSync(KEY.FARMER_PROFILE, data.profile);
        }
      })
      .catch(() => {
        // 离线或未建档时静默，保留本地展示
      });
  },

  /** 把一条作物记录转成页面用的结构（含流程类型与阶段词表） */
  buildCropItem(c: any): any {
    const pt = c.processType || inferProcess(c.cropName || c.name || '');
    return {
      id: genId(),
      processType: pt,
      name: c.cropName || c.name || '',
      cropVariety: c.cropVariety || '',
      plantMu: c.plantMu != null && c.plantMu !== '' ? String(c.plantMu) : '',
      sowingDate: c.sowingDate || '',
      growthStage: c.growthStage || '',
      stageOptions: STAGE_BY_PROCESS[pt] || [],
    };
  },

  /** 将档案数据回填到表单 */
  fillForm(p: any) {
    const expOpt = this.data.experienceOptions.find((o: any) => o.value === (p.experienceYear || 0));
    this.setData({
      form: {
        realName: p.realName || '',
        gender: p.gender || 0,
        age: p.age != null ? String(p.age) : '',
        phone: p.phone || '',
        experienceYear: p.experienceYear || 0,
        experienceYearText: expOpt ? expOpt.label : '',
        province: p.province || '',
        city: p.city || '',
        district: p.district || '',
        address: p.address || '',
        totalMu: p.totalMu != null ? String(p.totalMu) : '',
        cultivationType: p.cultivationType || 0,
        waterSource: p.waterSource || '',
        remark: p.remark || '',
      },
      selectedCrops: (p.crops || []).map((c: any) => this.buildCropItem(c)),
      remarkLength: (p.remark || '').length,
    });
  },

  /* ------------- 表单输入 ------------- */
  onInput(e: any) {
    const { key } = e.currentTarget.dataset;
    const value = e.detail.value;
    const patch: any = { [`form.${key}`]: value };
    if (key === 'remark') {
      patch.remarkLength = (value || '').length;
    }
    this.setData(patch);
  },

  /* ------------- 性别选择 ------------- */
  selectGender(e: any) {
    this.setData({ 'form.gender': Number(e.currentTarget.dataset.value) });
  },

  /* ------------- 种植经验 ------------- */
  onExperienceChange(e: any) {
    const idx = Number(e.detail.value);
    const opt = this.data.experienceOptions[idx];
    this.setData({
      'form.experienceYear': opt.value,
      'form.experienceYearText': opt.label,
    });
  },

  /* ------------- 省市区 ------------- */
  onRegionChange(e: any) {
    const [province, city, district] = e.detail.value;
    this.setData({
      'form.province': province,
      'form.city': city,
      'form.district': district,
    });
  },

  /* ------------- 种植类型 ------------- */
  selectCultivation(e: any) {
    this.setData({ 'form.cultivationType': Number(e.currentTarget.dataset.value) });
  },

  /* ------------- 灌溉水源 ------------- */
  onWaterChange(e: any) {
    const idx = Number(e.detail.value);
    this.setData({ 'form.waterSource': this.data.waterOptions[idx] });
  },

  /* ------------- 添加作物：先选流程类型 ------------- */
  onPendingProcessChange(e: any) {
    const opt = this.data.processOptions[Number(e.detail.value)];
    const patch: any = {
      pendingProcessType: opt.value,
      pendingProcessLabel: opt.label,
    };
    if (opt.value !== 'other') {
      patch.pendingCropName = '';
    }
    this.setData(patch);
  },

  onPendingCropNameInput(e: any) {
    this.setData({ pendingCropName: e.detail.value });
  },

  addCrop() {
    const pt = this.data.pendingProcessType;
    if (!pt) {
      wx.showToast({ title: '请选择作物类型', icon: 'none' });
      return;
    }
    let name = this.data.processLabels[pt];
    if (pt === 'other') {
      name = (this.data.pendingCropName || '').trim();
      if (!name) {
        wx.showToast({ title: '请输入作物名称', icon: 'none' });
        return;
      }
    }
    if (this.data.selectedCrops.some((c: any) => c.name === name)) {
      wx.showToast({ title: '该作物已添加', icon: 'none' });
      return;
    }
    const newCrop = {
      id: genId(),
      processType: pt,
      name,
      cropVariety: '',
      plantMu: '',
      sowingDate: '',
      growthStage: '',
      stageOptions: STAGE_BY_PROCESS[pt] || [],
    };
    this.setData({ selectedCrops: [...this.data.selectedCrops, newCrop], pendingCropName: '' });
  },

  quickAddCrop(e: any) {
    const name = e.currentTarget.dataset.name;
    if (this.data.selectedCrops.some((c: any) => c.name === name)) {
      wx.showToast({ title: '该作物已添加', icon: 'none' });
      return;
    }
    const pt = inferProcess(name);
    const newCrop = {
      id: genId(),
      processType: pt,
      name,
      cropVariety: '',
      plantMu: '',
      sowingDate: '',
      growthStage: '',
      stageOptions: STAGE_BY_PROCESS[pt] || [],
    };
    this.setData({ selectedCrops: [...this.data.selectedCrops, newCrop] });
  },

  removeCrop(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    const list = [...this.data.selectedCrops];
    list.splice(idx, 1);
    this.setData({ selectedCrops: list });
  },

  /** 切换某条作物的流程类型：同步展示名与阶段词表 */
  onProcessChange(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    const opt = this.data.processOptions[Number(e.detail.value)];
    const crop = this.data.selectedCrops[idx];
    const stageOptions = STAGE_BY_PROCESS[opt.value] || [];
    const keepStage = stageOptions.includes(crop.growthStage) ? crop.growthStage : '';
    this.setData({
      [`selectedCrops[${idx}].processType`]: opt.value,
      // 非“其他”时展示名由流程类型决定；切到“其他”时清空等农户填写
      [`selectedCrops[${idx}].name`]: opt.value === 'other' ? '' : opt.label,
      [`selectedCrops[${idx}].stageOptions`]: stageOptions,
      [`selectedCrops[${idx}].growthStage`]: keepStage,
    });
  },

  onCropDetailInput(e: any) {
    const { index, key } = e.currentTarget.dataset;
    this.setData({ [`selectedCrops[${index}].${key}`]: e.detail.value });
  },

  onCropNameInput(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    this.setData({ [`selectedCrops[${idx}].name`]: e.detail.value });
  },

  onSowingDateChange(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    this.setData({ [`selectedCrops[${idx}].sowingDate`]: e.detail.value });
  },

  onStageChange(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    const stageIdx = Number(e.detail.value);
    const crop = this.data.selectedCrops[idx];
    this.setData({ [`selectedCrops[${idx}].growthStage`]: (crop.stageOptions || [])[stageIdx] });
  },

  /* ------------- 校验 ------------- */
  validate(): string | null {
    const f = this.data.form;
    if (!f.realName.trim()) return '请输入真实姓名';
    if (f.gender !== 1 && f.gender !== 2) return '请选择性别';
    if (!/^1\d{10}$/.test(f.phone)) return '请输入正确的手机号';
    if (!f.experienceYear && f.experienceYear !== 0 || !f.experienceYearText) return '请选择种植经验';
    if (!f.province || !f.city || !f.district) return '请选择所在地区';
    if (!f.totalMu || Number(f.totalMu) <= 0) return '请输入总种植面积（亩）';
    if (![1, 2, 3].includes(f.cultivationType)) return '请选择种植类型';
    if (this.data.selectedCrops.length === 0) return '请至少选择一种种植作物';
    const badCrop = this.data.selectedCrops.find((c: any) => !c.processType || !c.name);
    if (badCrop) return '请完善种植作物的类型与名称';
    return null;
  },

  /* ------------- 提交档案 ------------- */
  submitProfile() {
    const err = this.validate();
    if (err) {
      wx.showToast({ title: err, icon: 'none' });
      return;
    }

    const token = wx.getStorageSync(KEY.TOKEN);
    if (!token) {
      wx.showToast({ title: '登录已过期，请重新登录', icon: 'none' });
      setTimeout(() => wx.reLaunch({ url: '/pages/login/login' }), 1500);
      return;
    }

    const f = this.data.form;
    const payload = {
      realName: f.realName.trim(),
      gender: f.gender,
      age: f.age ? Number(f.age) : null,
      phone: f.phone.trim(),
      experienceYear: f.experienceYear,
      province: f.province,
      city: f.city,
      district: f.district,
      address: f.address.trim(),
      totalMu: Number(f.totalMu),
      cultivationType: f.cultivationType,
      waterSource: f.waterSource || '',
      remark: f.remark.trim(),
      crops: this.data.selectedCrops.map((c: any) => ({
        cropName: c.name,
        processType: c.processType || '',
        cropVariety: c.cropVariety || null,
        plantMu: c.plantMu ? Number(c.plantMu) : null,
        sowingDate: c.sowingDate || null,
        growthStage: c.growthStage || '',
      })),
    };

    // 先存入本地缓存（无论后端是否就绪，本地数据不丢）
    wx.setStorageSync(KEY.FARMER_PROFILE, payload);
    wx.setStorageSync(KEY.FARMER_CROPS, payload.crops);
    wx.setStorageSync(KEY.FARMER_NAME, payload.realName);

    this.setData({ loading: true });

    const gotoHome = () => {
      this.setData({ loading: false });
      wx.reLaunch({ url: '/pages/index/index' });
    };

    saveProfile(payload)
      .then(() => {
        wx.showToast({ title: '档案保存成功', icon: 'success' });
        setTimeout(gotoHome, 1500);
      })
      .catch((err) => {
        // 离线/后端失败：本地已保存，仍可进入首页
        wx.showToast({ title: (err && err.message) || '网络异常，已本地保存', icon: 'none' });
        setTimeout(gotoHome, 1500);
      });
  },
});
