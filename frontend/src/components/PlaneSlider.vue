<script setup>
import { computed, onBeforeUnmount, watch } from 'vue'
import { useConvertStore } from '../store'

// A plane's position along the resolved axis: a number box and a slider
// over the part's whole side, in mm from the box centre (Fusion's slider).
// With `pickKey` (the store key the v-model binds) a crosshair button sits
// beside the box: press it, then click a face in the 3D view, and the
// plane moves to that face.
const props = defineProps({
  modelValue: { type: Number, default: 0 },
  id: { type: String, required: true },
  label: { type: String, default: 'Plane' },
  pickKey: { type: String, default: null },
})
const emit = defineEmits(['update:modelValue'])

const store = useConvertStore()
const step = computed(() => {
  const h = store.sliceHalfExtent
  return h > 50 ? 0.5 : h > 10 ? 0.1 : 0.05
})
const armed = computed(() => !!props.pickKey && store.planePick === props.pickKey)
const note = computed(() =>
  props.pickKey && store.planePickNote?.key === props.pickKey ? store.planePickNote.text : null)
const set = (v) => {
  if (!Number.isFinite(v)) return
  // moved by hand: the note about the last pick no longer holds
  if (note.value) store.planePickNote = null
  emit('update:modelValue', v)
}
const togglePick = () => { store.planePick = armed.value ? null : props.pickKey }
// a new axis moves every plane's meaning, and a slider that is gone (the
// tool changed) cannot take the pick
watch(() => store.resolvedSliceAxis, () => { if (armed.value) store.planePick = null })
onBeforeUnmount(() => {
  if (armed.value) store.planePick = null
  if (note.value) store.planePickNote = null
})
</script>

<template>
  <div class="row">
    <label :for="id + '_num'">
      {{ label }}
      <span class="unit num">mm from centre</span>
    </label>
    <span class="box">
      <button
        v-if="pickKey" type="button" class="pick" :class="{ on: armed }"
        :aria-pressed="armed"
        :title="armed ? 'Waiting for a face click in the 3D view (Esc cancels)' : `Pick a face in the 3D view to put the ${label.toLowerCase()} on`"
        @click="togglePick"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <circle cx="8" cy="8" r="4.5" fill="none" stroke="currentColor" stroke-width="1.4" />
          <path d="M8 0.5v4M8 11.5v4M0.5 8h4M11.5 8h4" stroke="currentColor" stroke-width="1.4" />
        </svg>
      </button>
      <input
        :id="id + '_num'" type="number" :step="step"
        :min="-store.sliceHalfExtent" :max="store.sliceHalfExtent"
        :value="modelValue"
        @input="set($event.target.valueAsNumber)"
      />
    </span>
  </div>
  <input
    :id="id" class="slider" type="range" :step="step"
    :min="-store.sliceHalfExtent" :max="store.sliceHalfExtent"
    :value="modelValue"
    :aria-label="`${label} along ${(store.resolvedSliceAxis || '').toUpperCase()}`"
    @input="set($event.target.valueAsNumber)"
  />
  <p v-if="armed" class="hint pickhint">Click a face in the 3D view · Esc cancels</p>
  <p v-else-if="note" class="hint num">{{ label }} {{ note }}</p>
</template>

<style scoped>
.box { display: inline-flex; align-items: center; gap: 6px; }
.pick {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0;
  border: 1px solid var(--line); border-radius: 4px;
  background: none; color: var(--muted); cursor: pointer;
}
.pick:hover { border-color: var(--edge); color: var(--edge); }
.pick.on { border-color: var(--edge); color: var(--edge); background: rgba(90, 210, 234, 0.18); }
.pickhint { color: var(--edge); }
</style>
