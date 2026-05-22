<template>
  <div class="analytics-chart" v-loading="loading">
    <v-chart :option="option" :style="{ height: height + 'px', width: '100%' }" autoresize />
  </div>
</template>

<script setup>
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
])

defineProps({
  option: {
    type: Object,
    required: true,
  },
  height: {
    type: Number,
    default: 300,
  },
  loading: {
    type: Boolean,
    default: false,
  },
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.analytics-chart {
  width: 100%;
}
</style>
