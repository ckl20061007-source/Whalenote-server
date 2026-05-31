-- ===========================================
-- 鲸记 · 微信公众号绑定 — 数据库初始化 SQL
-- 在 Supabase SQL Editor 中执行
-- ===========================================

-- 1. 绑定码表（App 端生成，微信端验证）
CREATE TABLE IF NOT EXISTS binding_codes (
  code VARCHAR(6) PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  used BOOLEAN DEFAULT FALSE
);

-- RLS: 用户只能读自己的绑定码
ALTER TABLE binding_codes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "bc_select_owner" ON binding_codes FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "bc_insert_owner" ON binding_codes FOR INSERT WITH CHECK (auth.uid() = user_id);

-- 2. 微信绑定表（openid ↔ user_id 映射）
CREATE TABLE IF NOT EXISTS wechat_bindings (
  openid VARCHAR(64) PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  bound_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE wechat_bindings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "wb_select_owner" ON wechat_bindings FOR SELECT USING (auth.uid() = user_id);

-- 3. transactions 表新增 source 字段
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'app';
