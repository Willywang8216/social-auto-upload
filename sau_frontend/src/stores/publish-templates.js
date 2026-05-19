import { defineStore } from 'pinia'
import { ref } from 'vue'

import { publishTemplatesApi } from '@/api/publish-templates'

export const usePublishTemplatesStore = defineStore('publish-templates', () => {
  const templates = ref([])
  const loaded = ref(false)

  async function refresh() {
    const response = await publishTemplatesApi.list()
    templates.value = response?.data?.templates || []
    loaded.value = true
    return templates.value
  }

  async function create(payload) {
    const response = await publishTemplatesApi.create(payload)
    const template = response?.data
    if (template) {
      templates.value = [template, ...templates.value.filter((t) => t.id !== template.id)]
    }
    return template
  }

  async function update(templateId, payload) {
    const response = await publishTemplatesApi.update(templateId, payload)
    const template = response?.data
    if (template) {
      templates.value = templates.value.map((t) => (t.id === templateId ? template : t))
    }
    return template
  }

  async function remove(templateId) {
    await publishTemplatesApi.delete(templateId)
    templates.value = templates.value.filter((t) => t.id !== templateId)
  }

  return { templates, loaded, refresh, create, update, remove }
})
