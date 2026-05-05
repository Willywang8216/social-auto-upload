<template>
  <div>
    <div class="connection-panel-card">
      <div v-for="row in rows" :key="row.label" class="health-row">
        <span>{{ row.label }}</span>
        <strong>{{ row.value || '—' }}</strong>
      </div>
    </div>
    <div v-if="actions.length > 0" class="connection-panel-actions">
      <el-button
        v-for="action in actions"
        :key="action.label"
        :type="action.type || 'default'"
        :plain="action.plain !== false"
        :disabled="Boolean(action.disabled)"
        @click="action.onClick && action.onClick()"
      >{{ action.label }}</el-button>
    </div>
  </div>
</template>

<script setup>
defineProps({
  rows: {
    type: Array,
    default: () => []
  },
  actions: {
    type: Array,
    default: () => []
  }
})
</script>

<style lang="scss" scoped>
.connection-panel-card {
  width: 100%;
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px;

  .health-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;

    &:last-child {
      margin-bottom: 0;
    }

    span {
      color: #909399;
    }

    strong {
      text-align: right;
      word-break: break-word;
    }
  }
}

.connection-panel-actions {
  margin-top: 12px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
