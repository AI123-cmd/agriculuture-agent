import { getProfile, saveProfile } from '../../api/farmer'
import { KEY } from '../../utils/constants'

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

    // 常用作物品类（快捷选择用）
    cropList: ['水稻', '小麦', '玉米', '辣椒', '烟草', '番茄', '黄瓜', '茄子', '大豆', '棉花', '油菜', '茶叶', '葡萄', '西瓜'],

    // 手动输入的作物名称
    cropInput: '',

    // 已添加作物（含详细信息）
    selectedCrops: [] as any[],

    // 生长阶段选项
    growthStages: ['育苗期', '移栽期', '苗期', '分蘖期', '拔节期', '抽穗期', '开花期', '灌浆期', '成熟期', '结果期', '膨大期', '采收期'],

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
      selectedCrops: (p.crops || []).map((c: any) => ({
        name: c.cropName,
        plantMu: c.plantMu != null ? String(c.plantMu) : '',
        growthStage: c.growthStage || '',
      })),
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

  /* ------------- 作物品类 ------------- */
  onCropInputText(e: any) {
    this.setData({ cropInput: e.detail.value });
  },

  addCrop() {
    const name = (this.data.cropInput || '').trim();
    if (!name) {
      wx.showToast({ title: '请输入作物名称', icon: 'none' });
      return;
    }
    // 去重
    if (this.data.selectedCrops.some((c: any) => c.name === name)) {
      wx.showToast({ title: '该作物已添加', icon: 'none' });
      return;
    }
    const newCrop = {
      name,
      plantMu: '',
      growthStage: '',
    };
    this.setData({
      selectedCrops: [...this.data.selectedCrops, newCrop],
      cropInput: '',
    });
  },

  quickAddCrop(e: any) {
    const name = e.currentTarget.dataset.name;
    if (this.data.selectedCrops.some((c: any) => c.name === name)) {
      wx.showToast({ title: '该作物已添加', icon: 'none' });
      return;
    }
    const newCrop = { name, plantMu: '', growthStage: '' };
    this.setData({ selectedCrops: [...this.data.selectedCrops, newCrop] });
  },

  removeCrop(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    const list = [...this.data.selectedCrops];
    list.splice(idx, 1);
    this.setData({ selectedCrops: list });
  },

  onCropDetailInput(e: any) {
    const { index, key } = e.currentTarget.dataset;
    this.setData({ [`selectedCrops[${index}].${key}`]: e.detail.value });
  },

  onStageChange(e: any) {
    const idx = Number(e.currentTarget.dataset.index);
    const stageIdx = Number(e.detail.value);
    this.setData({ [`selectedCrops[${idx}].growthStage`]: this.data.growthStages[stageIdx] });
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
        plantMu: c.plantMu ? Number(c.plantMu) : null,
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
