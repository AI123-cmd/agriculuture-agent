/** 集中管理本地缓存 key，消除散落的字符串 */
export const KEY = {
  TOKEN: 'token',
  USER_ID: 'userId',
  USER_INFO: 'userInfo',
  HAS_LOGIN: 'hasLogin',
  FARMER_PROFILE: 'farmerProfile',
  FARMER_CROPS: 'farmerCrops',
  FARMER_NAME: 'farmerName',
  FARM_LOCATION: 'farmLocation',
  CONVERSATION_LIST: 'ai_conversation_list',
  PENDING_SESSION_ID: 'pending_session_id',
  PENDING_CHAT_MODE: 'pending_chat_mode',
} as const