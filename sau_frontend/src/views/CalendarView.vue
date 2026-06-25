<template>
  <div class="calendar-view">
    <!-- Calendar header -->
    <div class="cal-head">
      <div class="cal-title">{{ MONTHS[view.m] }} {{ view.y }}</div>
      <div class="cal-nav">
        <button class="cal-nav-btn" @click="move(-1)" title="Previous">
          <component :is="icons.collapse" :width="15" :height="15" />
        </button>
        <button class="cal-nav-btn cal-today" @click="setToday">Today</button>
        <button class="cal-nav-btn" @click="move(1)" title="Next">
          <component :is="icons.expand" :width="15" :height="15" />
        </button>
      </div>
      <div class="spacer"></div>
      <router-link to="/publish/compose" class="btn-primary">
        <component :is="icons.plus" :width="16" :height="16" /> Schedule post
      </router-link>
    </div>

    <!-- Calendar grid -->
    <div class="cal-grid">
      <div v-for="dow in DOW" :key="dow" class="cal-dow">{{ dow }}</div>
      <div
        v-for="(cell, i) in cells"
        :key="i"
        class="cal-cell"
        :class="{ dim: cell.dim, today: cell.today }"
      >
        <div class="cal-date">
          <span v-if="cell.today" class="dn">{{ cell.d }}</span>
          <span v-else>{{ cell.dim ? '' : cell.d }}</span>
        </div>
        <div
          v-for="(ev, j) in cell.evs"
          :key="j"
          class="cal-ev"
          :class="{ err: ev.status === 'err' }"
          :title="ev.title + ' · ' + ev.time"
        >
          <span class="cd"></span>
          <span class="ct">{{ ev.time }}</span>
          <span class="cl">{{ ev.title }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { icons } from '@/utils/icons'

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']

const today = new Date()
const view = ref({ y: today.getFullYear(), m: today.getMonth() })

const first = computed(() => new Date(view.value.y, view.value.m, 1).getDay())
const days = computed(() => new Date(view.value.y, view.value.m + 1, 0).getDate())
const prevDays = computed(() => new Date(view.value.y, view.value.m, 0).getDate())

const cells = computed(() => {
  const result = []
  const start = prevDays.value - first.value + 1
  for (let i = 0; i < first.value; i++) result.push({ d: start + i, dim: true, evs: [], today: false })
  for (let d = 1; d <= days.value; d++) {
    const isToday = view.value.y === today.getFullYear() && view.value.m === today.getMonth() && d === today.getDate()
    result.push({ d, dim: false, today: isToday, evs: eventsForDay(d) })
  }
  while (result.length % 7) result.push({ d: 1, dim: true, evs: [], today: false })
  return result
})

const SCHEDULE = [
  { day: 3,  time: '09:00', title: 'Q2 teaser',          plats: ['douyin', 'tiktok'],            status: 'sched' },
  { day: 3,  time: '20:00', title: 'Founder story',      plats: ['youtube', 'bilibili'],          status: 'sched' },
  { day: 8,  time: '12:00', title: 'Recipe reel #14',    plats: ['xiaohongshu', 'douyin'],        status: 'sched' },
  { day: 12, time: '10:00', title: 'Weekend giveaway',    plats: ['douyin', 'kuaishou'],           status: 'sched' },
  { day: 12, time: '18:30', title: 'Studio BTS',        plats: ['youtube', 'tiktok'],            status: 'sched' },
  { day: 16, time: '09:00', title: 'Weekly digest',      plats: ['medium', 'substack'],           status: 'err'   },
  { day: 19, time: '15:00', title: 'Product FAQ',        plats: ['xiaohongshu'],                 status: 'sched' },
  { day: 24, time: '11:00', title: 'Launch announce',    plats: ['channels', 'douyin', 'tiktok'],  status: 'sched' },
  { day: 24, time: '19:00', title: 'Tutorial — setup',   plats: ['youtube'],                     status: 'sched' },
  { day: 27, time: '13:00', title: 'Recipe reel #15',   plats: ['xiaohongshu', 'douyin', 'kuaishou'], status: 'sched' },
]

const eventsForDay = (d) => SCHEDULE.filter(e => e.day === d)

const move = (delta) => {
  let m = view.value.m + delta
  let y = view.value.y
  if (m < 0) { m = 11; y-- }
  if (m > 11) { m = 0; y++ }
  view.value = { y, m }
}

const setToday = () => { view.value = { y: today.getFullYear(), m: today.getMonth() } }
</script>

<style scoped>
.calendar-view {
  padding: var(--space-6);
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.cal-head {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
  flex-shrink: 0;
}

.cal-title {
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -.02em;
}

.cal-nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.cal-nav-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--line);
  background: none;
  border-radius: var(--r-md);
  color: var(--text-2);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.cal-nav-btn:hover {
  background: var(--raised);
  color: var(--text);
}
.cal-today {
  width: auto;
  padding: 0 12px;
  font-size: 12.5px;
  font-weight: 500;
}

.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  grid-template-rows: auto 1fr;
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
  flex: 1;
  min-height: 0;
}

.cal-dow {
  background: var(--panel-2);
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  text-align: center;
  letter-spacing: .05em;
  text-transform: uppercase;
  line-height: 1;
}

.cal-cell {
  background: var(--panel);
  padding: 8px;
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 3px;
  transition: background var(--transition-fast);
}
.cal-cell:hover {
  background: var(--panel-2);
}
.cal-cell.dim {
  background: var(--raised);
  opacity: 0.5;
}
.cal-cell.today {
  background: var(--accent-soft);
}

.cal-date {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-2);
  margin-bottom: 2px;
}
.cal-cell.today .cal-date .dn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  background: var(--accent);
  color: var(--accent-ink);
  border-radius: 50%;
  font-weight: 700;
  font-size: 11px;
}

.cal-ev {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--text-2);
  overflow: hidden;
  border-radius: 4px;
  padding: 1px 4px;
  background: var(--raised);
  cursor: default;
}
.cal-ev.err {
  background: var(--color-danger-light);
  color: var(--color-danger);
}
.cd {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
}
.cal-ev.err .cd { background: var(--color-danger); }
.ct { flex-shrink: 0; font-family: var(--font-mono); font-size: 10px; }
.cl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.spacer { flex: 1; }
</style>