// pages/device/bind.ts
import { bindDevice, parseSample, saveMapping } from '../../api/device'
import { ensureAuth } from '../../utils/guard'
import type { DetectedField, MappingItem, MappingPreview } from '../../types/api'

/** 标准指标选项（与后端 StandardMetric / METRIC_LABELS 对齐），首项为“忽略此字段” */
const METRIC_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '忽略此字段' },
  { value: 'soil_temp', label: '土壤温度' },
  { value: 'soil_moisture', label: '土壤水分' },
  { value: 'air_temp', label: '空气温度' },
  { value: 'air_humidity', label: '空气湿度' },
  { value: 'rainfall', label: '降雨量' },
  { value: 'light_lux', label: '光照' },
  { value: 'wind_speed', label: '风速' },
  { value: 'soil_ph', label: '土壤pH' },
  { value: 'soil_ec', label: '土壤EC' },
]

interface FieldRow {
  rawField: string
  sampleValue: string
  metricIndex: number
  metricValue: string
  confidence: number
}

/** 依据后端推断结果选择默认指标下标（未推断出则默认“忽略”） */
function defaultMetricIndex(inferred: string | null | undefined): number {
  if (!inferred) return 0;
  const idx = METRIC_OPTIONS.findIndex((o) => o.value === inferred);
  return idx >= 0 ? idx : 0;
}

Page({
  data: {
    step: 1,
    metricOptions: METRIC_OPTIONS,

    // 步骤一：绑定
    deviceSn: '',
    binding: false,

    // 步骤一结果
    deviceId: '',
    reportUrl: '',
    deviceSecret: '',
    templateMatched: false,

    // 步骤二：样例报文
    payload: '',
    parsing: false,
    parsed: false,
    inferError: '',

    // 步骤三：字段映射
    fields: [] as FieldRow[],
    saving: false,
    warnings: [] as string[],
    preview: [] as MappingPreview[],
    savedCount: 0,
    done: false,
  },

  onLoad(query: Record<string, string>) {
    if (!ensureAuth()) return;
    // 从设备列表“继续接入”进入：已有 deviceId，直接跳到步骤二
    if (query && query.deviceId) {
      this.setData({
        deviceId: query.deviceId,
        deviceSn: query.deviceSn ? decodeURIComponent(query.deviceSn) : '',
        step: 2,
      });
    }
  },

  goBack() {
    if (this.data.step > 1 && !this.data.done) {
      this.setData({ step: this.data.step - 1 });
      return;
    }
    wx.navigateBack({ delta: 1 });
  },

  /* ---------- 步骤一：绑定 ---------- */
  onSnInput(e: WechatMiniprogram.Input) {
    this.setData({ deviceSn: e.detail.value });
  },

  submitBind() {
    const sn = this.data.deviceSn.trim();
    if (!sn) {
      wx.showToast({ title: '请输入设备编号', icon: 'none' });
      return;
    }
    if (this.data.binding) return;
    this.setData({ binding: true });
    bindDevice({ deviceSn: sn })
      .then((res) => {
        this.setData({
          deviceId: res.deviceId,
          reportUrl: res.reportUrl || '',
          deviceSecret: res.deviceSecret || '',
          templateMatched: res.vendorTemplateMatched,
          step: 2,
        });
        if (!res.deviceSecret) {
          wx.showToast({ title: '设备此前已绑定', icon: 'none' });
        }
      })
      .catch((err) => {
        wx.showToast({ title: (err && err.message) || '绑定失败', icon: 'none' });
      })
      .finally(() => {
        this.setData({ binding: false });
      });
  },

  copyValue(e: WechatMiniprogram.BaseEvent) {
    const value = e.currentTarget.dataset.value as string;
    if (!value) return;
    wx.setClipboardData({ data: value });
  },

  /* ---------- 步骤二：样例报文 ---------- */
  onPayloadInput(e: WechatMiniprogram.TextareaInput) {
    this.setData({ payload: e.detail.value });
  },

  submitParse() {
    const payload = this.data.payload.trim();
    if (!payload) {
      wx.showToast({ title: '请粘贴样例报文', icon: 'none' });
      return;
    }
    if (this.data.parsing) return;
    this.setData({ parsing: true });
    parseSample(this.data.deviceId, payload)
      .then((res) => {
        if (!res.parsed) {
          this.setData({ parsed: false, inferError: res.inferError || '报文无法识别，请核对格式' });
          return;
        }
        const fields: FieldRow[] = (res.fields || []).map((f: DetectedField) => {
          const idx = defaultMetricIndex(f.inferredMetric);
          return {
            rawField: f.rawField,
            sampleValue: f.sampleValue,
            metricIndex: idx,
            metricValue: METRIC_OPTIONS[idx].value,
            confidence: f.confidence,
          };
        });
        this.setData({ parsed: true, inferError: '', fields, step: 3 });
      })
      .catch((err) => {
        this.setData({ parsed: false, inferError: (err && err.message) || '解析失败' });
      })
      .finally(() => {
        this.setData({ parsing: false });
      });
  },

  /* ---------- 步骤三：字段映射 ---------- */
  onMetricChange(e: WechatMiniprogram.PickerChange) {
    const index = Number(e.currentTarget.dataset.index);
    const pickIdx = Number(e.detail.value);
    const fields = this.data.fields.slice();
    fields[index] = {
      ...fields[index],
      metricIndex: pickIdx,
      metricValue: METRIC_OPTIONS[pickIdx].value,
    };
    this.setData({ fields });
  },

  submitMapping() {
    const mappings: MappingItem[] = this.data.fields
      .filter((f) => f.metricValue)
      .map((f) => ({
        rawField: f.rawField,
        standardMetric: f.metricValue,
        sampleRaw: f.sampleValue,
      }));

    if (mappings.length === 0) {
      wx.showToast({ title: '请至少为一个字段选择含义', icon: 'none' });
      return;
    }
    if (this.data.saving) return;
    this.setData({ saving: true });
    saveMapping({ deviceId: this.data.deviceId, mappings })
      .then((res) => {
        this.setData({
          warnings: res.warnings || [],
          preview: res.preview || [],
          savedCount: res.saved,
          done: true,
        });
      })
      .catch((err) => {
        wx.showToast({ title: (err && err.message) || '保存失败', icon: 'none' });
      })
      .finally(() => {
        this.setData({ saving: false });
      });
  },

  goDeviceList() {
    wx.redirectTo({ url: '/pages/device/list' });
  },
});
