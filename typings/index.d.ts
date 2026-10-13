/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    userInfo?: WechatMiniprogram.UserInfo,
    /** 首页/历史页点"新对话"时置位；问答页 onShow 读到即清空上一轮会话 */
    pendingNewChat?: boolean,
  }
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback,
}