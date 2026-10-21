<script setup>
import { computed, onBeforeUnmount, watch } from 'vue'
import { useConvertStore, DEFAULT_PARAMS } from '../store'
import { summarise } from '../sectionSummary'
import AxisSelect from './AxisSelect.vue'
import PlaneSlider from './PlaneSlider.vue'
import SlicePlaneOptions from './SlicePlaneOptions.vue'
import GateFields from './GateFields.vue'

const store = useConvertStore()
const isDefault = (key) => store.params[key] === DEFAULT_PARAMS[key]

// Single slice: the plane follows the slider at once; the trace (a cut
// plus a curve fit, a few seconds on a big mesh) runs when the slider
// rests. Any change of axis, tolerance, scale or gap joining re-traces
// too. The panel mounts when the tool is picked, so the first trace runs
// at once (immediate).
let traceTimer = null
const scheduleTrace = () => {
  clearTimeout(traceTimer)
  traceTimer = setTimeout(() => store.traceSection(), 350)
}
watch(() => [store.sliceOffset, store.resolvedSliceAxis, store.params.tol, store.params.units,
             store.params.scale, store.jobId, store.params.slice_join, store.sliceOutline, store.params.slice_trim],
      ([, axis, , , , job]) => {
        // the old outline belongs to the old plane: drop it at once, so the
        // view never shows a trace that does not sit on the plane
        store.section = null
        if (!job || !axis) return
        scheduleTrace()
      }, { immediate: true })
onBeforeUnmount(() => clearTimeout(traceTimer))

// The auto axis is worked out from the slicing structure as soon as the
// tool is up (and again when the spacing, units, scale or gap joining
// change), so the plane shows on the axis the run will use.
let axisTimer = null
watch(() => [store.jobId, store.params.slice_axis, store.params.units, store.params.scale,
             store.params.slice_mm, store.params.slice_join, store.params.slice_trim],
      ([job, axis]) => {
        clearTimeout(axisTimer)
        if (!job || axis !== 'auto') return
        axisTimer = setTimeout(() => store.fetchLoftAxis(), 250)
      }, { immediate: true })
onBeforeUnmount(() => clearTimeout(axisTimer))

const sectionSummary = computed(() => summarise(store.section?.stats, store.sliceOutline))
</script>

<template>
  <section class="params">
    <p class="hint">
      For shapes that change smoothly along one axis (caps, handles, shells,
      bottles), for flat plates with a fancy outline sliced across their
      thin side, and for one smooth stretch of a mixed part with “Loft
      only”. Not for machined parts with slots, sideways holes and steps:
      Mesh → Solid does those with true planes and cylinders. Every
      section here becomes a spline, so a run is one B-spline face.
    </p>
    <div class="row">
      <label for="slice_mm">
        Slice spacing
        <span class="unit num">mm</span>
        <span class="tag gen">used by the loft</span>
      </label>
      <input
        id="slice_mm" type="number" step="0.1" min="0.05" max="50"
        v-model.number="store.params.slice_mm"
        :class="{ touched: !isDefault('slice_mm') }"
      />
    </div>
    <AxisSelect />
    <div v-if="store.sliceHalfExtent > 0" class="slice">
      <p class="previewhead">
        <span class="tag">preview</span>
        Check one slice before you loft. Nothing in this box is generated on
        its own; the button below lofts {{ store.params.slice_range_mm > 0 ? 'the stretch you set under Loft only' : 'the whole part' }}.
      </p>
      <PlaneSlider v-model="store.sliceOffset" id="slice_offset"
                   :label="store.params.slice_range_mm > 0 ? 'Loft starts at' : 'Preview slice'"
                   pick-key="sliceOffset" />
      <p class="hint">
        Move the slider and the plane in the 3D view follows; the traced
        outline is drawn on it a moment later (Fusion's Create Mesh Section
        Sketch + Fit Curves to Mesh Section, for one plane).
        <template v-if="store.params.slice_range_mm > 0">With Loft only set, this plane is also where the loft starts.</template>
        To download this slice as a Fusion sketch, use X-Ray with start = end.
      </p>
      <SlicePlaneOptions />
      <p v-if="store.sectionBusy" class="hint">tracing…</p>
      <p v-else-if="store.sectionError" class="hint warn">Could not trace: {{ store.sectionError }}</p>
      <p v-else-if="sectionSummary" class="hint num">Preview slice: {{ sectionSummary }}</p>
      <div class="row">
        <label for="slice_range">
          Loft only
          <span class="unit num">mm from this plane</span>
        </label>
        <input
          id="slice_range" type="number" step="1" min="0" max="2000"
          v-model.number="store.params.slice_range_mm"
          :class="{ touched: !isDefault('slice_range_mm') }"
        />
      </div>
      <div v-if="store.params.slice_range_mm > 0" class="row">
        <label for="slice_dir">Going</label>
        <select id="slice_dir" v-model="store.params.slice_range_dir"
                :class="{ touched: !isDefault('slice_range_dir') }">
          <option value="+">+ (along the axis arrow)</option>
          <option value="-">− (against it)</option>
        </select>
      </div>
      <p class="hint">
        <span class="tag gen">used by the loft</span>
        0 lofts the whole body. A length lofts just that stretch, starting
        at the slice plane above and going the chosen way, with flat ends:
        20 mm from the 36 mm plane going − gives a 36 → 16 mm slab. The
        check then measures only that stretch of the mesh.
      </p>
    </div>
    <label class="check loft">
      <input type="checkbox" v-model="store.params.loft_ruled" />
      <span>
        Ruled loft
        <span class="tag gen">used by the loft</span>
        <span class="help">Straight faces between neighbouring slices (one face per pair) instead of one smooth face per run. Exact within a hair at 0.2 mm spacing and never overshoots, but many more faces. The smooth loft is used by default and falls back to ruled on its own where a run's smooth surface comes out wrong.</span>
      </span>
    </label>
    <p class="hint">
      Slices are cut every {{ store.params.slice_mm }} mm; the loft itself goes through at most about 60 of them per run, and the checks below still measure the result against every triangle of the mesh. Steps (flat faces across the axis) and changes in the outline count split the stack into runs, so a shoulder stays a real flat face.
    </p>

    <GateFields :enforced="false" />
  </section>
</template>

<style scoped>
.previewhead { font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 10px; margin-top: 2px; }
</style>
