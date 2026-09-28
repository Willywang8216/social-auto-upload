<template>
  <div class="help-view">
    <header class="help-hero">
      <div class="hh-ic"><component :is="icons.help" /></div>
      <div class="hh-text">
        <h3>Publishing & schedule guide</h3>
        <p>Prepare, review, schedule, monitor, and recover posts across connected accounts.</p>
      </div>
    </header>

    <nav class="guide-nav" aria-label="Help topics">
      <a href="#create">Create and schedule</a><a href="#calendar">Calendar and queue</a>
      <a href="#media">Media and links</a><a href="#failures">Failures and recovery</a><a href="#telegram">Telegram</a>
    </nav>

    <section id="create" class="guide-section">
      <h2>Create and schedule a post</h2>
      <ol>
        <li>Open <router-link to="/publish/compose">Publish → Compose</router-link>, add the image set or video, then choose one or more profiles. A profile can publish to multiple connected accounts.</li>
        <li>Review the generated copy for each destination. Text can differ by platform/account; edit it before submitting.</li>
        <li>Choose Publish now or a future start time. Destinations may be staggered, so check the time shown on each destination.</li>
        <li>Submit once. Calendar and Queue group the shared media set as one entity and show per-destination progress in its details.</li>
      </ol>
      <p class="guide-note">Rescheduling is available only for pending/retrying targets. Cancelling retains history and does not interrupt an upload already running.</p>
    </section>

    <section id="calendar" class="guide-section">
      <h2>Calendar and queue</h2>
      <ul>
        <li><b>Calendar:</b> navigate months with the arrows or Today. Open an entity to see media, copy, destinations, schedules, statuses, and errors. “More” opens all items on busy days.</li>
        <li><b>Queue:</b> shows recent jobs. Use Load more to retrieve older history. Open a row to see target attempts and last errors.</li>
        <li><b>Pending / Retrying:</b> waiting for its scheduled time or another attempt. Reschedule or cancel from details.</li>
        <li><b>Running:</b> the worker is publishing. Copy and schedule changes are locked for that target.</li>
        <li><b>Succeeded / Failed / Cancelled:</b> final states. Fix the cause before resubmitting a failed target. Cancellation preserves the audit record.</li>
      </ul>
    </section>

    <section id="media" class="guide-section">
      <h2>Media previews and Drive links</h2>
      <p>Queued media may be local, in a private Drive cache, or on configured public storage. Local previews use the app’s media endpoint. A private Drive cache path is not a shareable link; only a public HTTPS link is shown externally. If media is missing, check the source and storage connection, restore or re-upload the file, then retry.</p>
    </section>

    <section id="failures" class="guide-section">
      <h2>Failures and recovery</h2>
      <div class="failure-list">
        <article><b>Login or session expired</b><p>Reconnect the named account under Accounts, confirm it is enabled, then resubmit that failed target.</p></article>
        <article><b>Media missing or download failed</b><p>Check the media name and remote storage connection. Restore or upload the source, then retry.</p></article>
        <article><b>Copy, format, or media constraint</b><p>Read the destination’s error, edit that platform’s copy or media, and retry only the affected target.</p></article>
        <article><b>Rate limit or temporary platform error</b><p>Retrying work is retried automatically. For a permanently failed target, wait for the platform limit window and resubmit.</p></article>
        <article><b>Schedule rejected or in the past</b><p>Choose a future time. Schedule records are stored in UTC; the displayed time uses the calendar’s local-time interpretation.</p></article>
      </div>
      <p>Inspect failures by destination: profile, platform/account, attempt count, and last error are shown together. Do not recreate the whole entity when just one destination failed.</p>
    </section>

    <section id="telegram" class="guide-section">
      <h2>Telegram notifications</h2>
      <p>Review cards may be sent when a post is submitted if configured. The daily digest is scheduled for 08:00 Asia/Shanghai and lists today’s scheduled entities with direct management links. Permanent-failure notices identify the destination, error, and recovery action. Configure the bot/chat for the backend process that runs scheduled work.</p>
      <p>Delivery is best-effort. If a notice is missing, inspect backend logs and confirm the Telegram alert token and chat ID are configured for the worker/scheduler.</p>
    </section>

    <footer class="help-links">
      <router-link to="/publish/compose">Open Publish Center</router-link>
      <router-link to="/publish/calendar">Open Calendar</router-link>
      <router-link to="/publish/queue">Open Queue</router-link>
      <router-link to="/accounts">Manage accounts</router-link>
    </footer>
  </div>
</template>

<script setup>
import { icons } from '@/utils/icons'
</script>

<style scoped>
.help-view {
  padding: var(--space-6);
  max-width: 1040px;
  margin: 0 auto;
}

.help-hero {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-6);
  background: var(--panel);
  border-radius: var(--r-xl);
  margin-bottom: var(--space-4);
  border: 1px solid var(--line);
}

.hh-ic {
  width: 48px;
  height: 48px;
  background: var(--accent-soft);
  border-radius: var(--r-lg);
  display: grid;
  place-items: center;
  color: var(--accent);
  flex-shrink: 0;
}

.hh-text h3 { font-size: 20px; margin: 0 0 4px; }
.hh-text p { color: var(--text-2); margin: 0; font-size: 13.5px; }

.guide-nav, .help-links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 16px 0 24px;
}

.guide-nav a, .help-links a { color: var(--accent); }

.guide-section {
  scroll-margin-top: 24px;
  padding: 22px 24px;
  margin: 14px 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
}

.guide-section h2 { font-size: 17px; margin: 0 0 12px; }
.guide-section p, .guide-section li { color: var(--text-2); line-height: 1.7; }
.guide-section li { margin: 6px 0; }
.guide-section a { color: var(--accent); }
.guide-note { padding: 12px; background: var(--raised); border-radius: 8px; }

.failure-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.failure-list article { padding: 12px; background: var(--raised); border-radius: 8px; }
.failure-list p { margin: 5px 0 0; font-size: 13px; }

@media (max-width: 680px) {
  .help-view { padding: 16px; }
  .help-hero { align-items: flex-start; }
  .failure-list { grid-template-columns: 1fr; }
}
</style>