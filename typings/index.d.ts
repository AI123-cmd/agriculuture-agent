/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    userInfo?: WechatMiniprogram.UserInfo,
    /** 首页/历史页点"新对话"时置位；问答页 onShow 读到即清空上一轮会话 */
    pendingNewChat?: boolean,
    /** 历史页点某条会话时置为该会话 id；问答页 onShow 读到即加载其历史记录 */
    pendingOpenSession?: string,
  }
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback,
}