<template>
  <div class="account-text-field-list">
    <el-form-item v-for="field in fields" :key="field.key" :label="field.label">
      <el-input
        :model-value="modelValue?.[field.key] ?? ''"
        :type="field.type || 'text'"
        :rows="field.rows || undefined"
        :placeholder="field.placeholder || ''"
        @update:model-value="(value) => emit('update-field', { key: field.key, value })"
      />
    </el-form-item>
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
