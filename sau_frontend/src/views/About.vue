<template>
  <div class="about">
    <el-card class="about-card">
      <div class="about-header">
        <h1>自媒體自動化營運系統</h1>
        <p class="version">social-auto-upload</p>
      </div>

      <el-divider />

      <div class="about-section">
        <h3>系統簡介</h3>
        <p>
          本系統是一款強大的自動化工具，協助內容創作者與營運人員一鍵把影片內容高效率發佈到多個海內外主流社群平台。
          支援影片上傳、定時發佈等功能。
        </p>
      </div>

      <div class="about-section">
        <h3>支援平台</h3>
        <div class="platform-tags">
          <el-tag type="danger">抖音</el-tag>
          <el-tag type="success">快手</el-tag>
          <el-tag type="warning">影片號</el-tag>
          <el-tag type="info">小紅書</el-tag>
        </div>
      </div>

      <div class="about-section">
        <h3>核心功能</h3>
        <ul class="feature-list">
          <li>多平台帳號管理與登入狀態維護</li>
          <li>影片素材上傳與管理</li>
          <li>一鍵多平台發佈</li>
          <li>定時發佈與批次發佈</li>
          <li>Cookie 导入导出</li>
        </ul>
      </div>

      <div class="about-section">
        <h3>技術棧</h3>
        <div class="tech-tags">
          <el-tag effect="plain">Vue 3</el-tag>
          <el-tag effect="plain">Element Plus</el-tag>
          <el-tag effect="plain">Pinia</el-tag>
          <el-tag effect="plain">Flask</el-tag>
          <el-tag effect="plain">Playwright</el-tag>
          <el-tag effect="plain">SQLite</el-tag>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
// 關於頁元件
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.about {
  max-width: 700px;
  margin: 0 auto;

  .about-card {
    .about-header {
      text-align: center;

      h1 {
        color: $text-primary;
        margin: 0 0 8px 0;
        font-size: 24px;
      }

      .version {
        color: $text-secondary;
        font-size: 14px;
        margin: 0;
      }
    }

    .about-section {
      margin-bottom: 24px;

      h3 {
        font-size: 16px;
        color: $text-primary;
        margin: 0 0 12px 0;
      }

      p {
        color: $text-secondary;
        line-height: 1.8;
        margin: 0;
      }

      .platform-tags,
      .tech-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }

      .feature-list {
        margin: 0;
        padding-left: 20px;
        color: $text-secondary;
        line-height: 2;
      }
    }
  }
}
</style>
e>
