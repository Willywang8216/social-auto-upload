<template>
  <fieldset class="kit-picker" :disabled="disabled">
    <legend>衍生套件主題</legend>
    <div class="swatches" role="radiogroup" aria-label="Teaching 主題">
      <button v-for="theme in themes" :key="theme.id" type="button" role="radio"
        :class="['swatch', theme.id, { selected: modelValue === theme.id }]"
        :aria-checked="modelValue === theme.id" :disabled="disabled"
        @click="$emit('update:modelValue', theme.id)">
        <span class="sample" aria-hidden="true">學</span>
        <span>{{ theme.label }}<small>{{ theme.description }}</small></span>
        <span v-if="modelValue === theme.id" class="check" aria-hidden="true">✓</span>
      </button>
    </div>
    <p>Noto Serif TC 標題、Noto Sans TC 內文與銅色點綴。更換主題會建立新的套件版本，仍須在 Notion 重新審閱。</p>
  </fieldset>
</template>

<script setup>
defineProps({ modelValue: { type: String, default: 'ink' }, disabled: Boolean })
defineEmits(['update:modelValue'])
const themes = [
  { id: 'ink', label: 'Ink', description: '深墨底・米白字' },
  { id: 'cream', label: 'Cream', description: '米白底・深墨字' },
]
</script>

<style scoped>
.kit-picker { margin: 0; padding: 0; border: 0; min-width: 0; }
legend { font-weight: 600; margin-bottom: 12px; color: var(--text); }
.swatches { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.swatch { position: relative; display: flex; gap: 12px; align-items: center; text-align: left;
  padding: 16px; border-radius: 12px; border: 2px solid transparent; transition: border-color .15s; }
.swatch.ink { background: #1c2330; color: #faf7f1; }
.swatch.cream { background: #faf7f1; color: #1c2330; }
.swatch.selected { border-color: #c0633f; }
.swatch:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.sample { font-family: 'Noto Serif TC', serif; font-size: 34px; color: #c0633f; }
small { display: block; opacity: .8; font-size: 12px; margin-top: 3px; }
.check { margin-left: auto; color: #c0633f; font-size: 18px; }
button:disabled { opacity: .65; cursor: not-allowed; }
p { color: var(--text-2); font-size: 12px; line-height: 1.8; margin: 12px 0 0; }
@media (max-width: 480px) { .swatches { grid-template-columns: 1fr; } }
</style>
