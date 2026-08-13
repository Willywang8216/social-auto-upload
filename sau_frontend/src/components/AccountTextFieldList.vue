<template>
  <div class="account-text-field-list">
    <div v-for="field in fields" :key="field.key" class="field" style="margin-top:14px">
      <label>{{ field.label }}</label>
      <el-input
        v-if="!field.type || field.type === 'text'"
        :model-value="modelValue?.[field.key] ?? ''"
        type="text"
        :placeholder="field.placeholder || ''"
        @update:model-value="(value) => emit('update-field', { key: field.key, value })"
      />
      <el-input
        v-else
        :model-value="modelValue?.[field.key] ?? ''"
        :type="field.type"
        :rows="field.rows || undefined"
        :placeholder="field.placeholder || ''"
        @update:model-value="(value) => emit('update-field', { key: field.key, value })"
      />
      <div v-if="field.hint" class="ath-hint">{{ field.hint }}</div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  fields: {
    type: Array,
    default: () => []
  },
  modelValue: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update-field'])
</script>

<style scoped>
.account-text-field-list {
  display: block;
}
.ath-hint {
  font-size: 11.5px;
  color: var(--text-3, #888);
  margin-top: 5px;
  line-height: 1.4;
}
</style>
