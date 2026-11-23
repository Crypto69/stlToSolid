<script setup>
import { computed, ref } from 'vue'
import { useConvertStore } from '../store'

const store = useConvertStore()

// the upload's mirror check: which axis to offer, and how well each matched
const sym = computed(() => store.symmetry)
const suggested = computed(() => sym.value?.suggested || null)
const planeFor = (a) => (sym.value?.planes || []).find((p) => p.axis === a) || null
const pct = (v) => `${Math.round(100 * v)}%`
const current = computed(() => (store.params.half ? planeFor(store.params.half) : null))
const custom = computed(() => store.params.half_at !== null && store.params.half_at !== '')

const on = computed({
  get: () => !!store.params.half,
  set: (v) => {
    store.params.half = v ? (store.params.half || suggested.value || 'x') : null
    if (!v) store.params.half_at = null
  },
})
function useHalf(axis) {
  store.params.half = axis
  store.params.half_at = null
}
const cutAt = computed({
  get: () => (custom.value ? store.params.half_at : ''),
  set: (v) => { store.params.half_at = v === '' || v === null || !Number.isFinite(Number(v)) ? null : Number(v) },
})
const where = computed(() => {
  if (!store.params.half) return ''
  const A = store.params.half.toUpperCase()
  const off = Math.abs(store.halfOffset) < 5e-4 ? 0 : store.halfOffset
  const how = custom.value ? 'the offset above'
    : store.mirrorPlane(store.params.half) ? 'the mirror plane found' : 'the box centre'
  return `${A} = ${off >= 0 ? '+' : ''}${off.toFixed(3)} mm from the centre (${how})`
})
const openInfo = ref(false)
</script>

<template>
  <section class="params half">
    <header class="head">
      <h2 class="micro">Half model</h2>
    </header>
    <p v-if="suggested && !store.params.half" class="hint">
      This part looks <b>{{ planeFor(suggested).symmetric ? '' : 'nearly ' }}mirror-symmetric
      about {{ suggested.toUpperCase() }}</b>
      ({{ pct(planeFor(suggested).match) }} of it lands on its mirror image).
      <button class="link" @click="useHalf(suggested)">Write only half</button>
    </p>
    <label class="check">
      <input type="checkbox" v-model="on" />
      <span>
        Write only half
        <button
          class="info num"
          :aria-expanded="openInfo"
          aria-label="What does Write only half do?"
          @click.prevent="openInfo = !openInfo"
        >i</button>
      </span>
    </label>
    <div v-if="openInfo" class="explain">
      <p>For a part that is the same on both sides, like a toy car. The part is converted as usual, then cut on the mirror plane, and only one side goes into the STEP and the scripts. In Fusion, use Mirror on the cut face to get the whole part back, or model on the half.</p>
      <p><span class="dir">The plane</span> goes where the part is mirror-symmetric (found when the file was uploaded), or at the box centre when it is not. Type an offset to put it somewhere else.</p>
    </div>
    <template v-if="store.params.half">
      <div class="row">
        <label for="half-axis">Mirror plane across</label>
        <select id="half-axis" v-model="store.params.half" class="touched">
          <option v-for="a in ['x', 'y', 'z']" :key="a" :value="a">
            {{ a.toUpperCase() }}{{ planeFor(a) ? (planeFor(a).symmetric ? ' · symmetric' : planeFor(a).nearly ? ' · nearly' : ` · ${pct(planeFor(a).match)}`) : '' }}
          </option>
        </select>
      </div>
      <div class="row">
        <label for="half-keep">Side kept</label>
        <select id="half-keep" v-model="store.params.half_keep">
          <option value="+">+{{ store.params.half.toUpperCase() }}</option>
          <option value="-">&minus;{{ store.params.half.toUpperCase() }}</option>
        </select>
      </div>
      <div class="row">
        <label for="half-at">Cut at, mm from centre</label>
        <input id="half-at" type="number" step="0.1" placeholder="auto"
               v-model="cutAt" :class="{ touched: custom }" />
      </div>
      <p class="hint">Cut at <span class="num">{{ where }}</span>, keeping the
        {{ store.params.half_keep === '-' ? '\u2212' : '+' }}{{ store.params.half.toUpperCase() }} side.</p>
      <p v-if="current && current.nearly" class="hint">
        Nearly symmetric: about {{ pct(1 - current.match) }} of the surface differs from its mirror
        image. The half keeps only this side's version of it.
      </p>
      <p v-if="current && !current.symmetric && !current.nearly" class="warn">
        <b>Not symmetric about {{ store.params.half.toUpperCase() }}.</b>
        Only {{ pct(current.match) }} of the part lands on its mirror image across this axis, so
        mirroring the half back will not give the original.
        <template v-if="suggested && suggested !== store.params.half">
          <button class="link" @click="useHalf(suggested)">Use {{ suggested.toUpperCase() }}</button>
        </template>
      </p>
    </template>
  </section>
</template>
