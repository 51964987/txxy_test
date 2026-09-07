<script setup lang="ts">
import { computed } from 'vue'
import {
  QUERY_FIELDS,
  fieldMeta,
  normalizeOperator,
  operatorsOf,
  type ConditionNode,
} from '../../utils/queryMeta'

/** 单条条件：字段 + 操作符 + 值（值控件随字段类型与操作符变化） */
const props = defineProps<{ model: ConditionNode; fidOptions: { fid: string; name: string }[] }>()
const emit = defineEmits<{ remove: [] }>()

const fieldType = computed(() => fieldMeta(props.model.field)?.type ?? 'text')
const operatorOptions = computed(() => operatorsOf(props.model.field))
const arity = computed(
  () => operatorOptions.value.find((o) => o.key === props.model.op)?.arity ?? 1,
)

function onFieldChange() {
  props.model.op = normalizeOperator(props.model.field, props.model.op)
  props.model.value = arity.value === 2 ? ['', ''] : arity.value === -1 ? [] : ''
}

function onOperatorChange() {
  props.model.value = arity.value === 2 ? ['', ''] : arity.value === -1 ? [] : ''
}

/** 多值（IN）：数组与逗号分隔字符串互转，统一按字符串数组存放 */
const multiValue = computed({
  get: () => (Array.isArray(props.model.value) ? props.model.value.join(',') : String(props.model.value ?? '')),
  set: (v: string) => {
    props.model.value = v.split(',').map((s) => s.trim()).filter((s) => s !== '')
  },
})

const rangeStart = computed({
  get: () => (Array.isArray(props.model.value) ? props.model.value[0] ?? '' : ''),
  set: (v: string) => {
    const end = Array.isArray(props.model.value) ? props.model.value[1] ?? '' : ''
    props.model.value = [v, end]
  },
})

const rangeEnd = computed({
  get: () => (Array.isArray(props.model.value) ? props.model.value[1] ?? '' : ''),
  set: (v: string) => {
    const start = Array.isArray(props.model.value) ? props.model.value[0] ?? '' : ''
    props.model.value = [start, v]
  },
})
</script>

<template>
  <div class="condition-row">
    <el-select v-model="model.field" size="small" class="c-field" @change="onFieldChange">
      <el-option v-for="f in QUERY_FIELDS" :key="f.key" :label="f.label" :value="f.key" />
    </el-select>

    <el-select v-model="model.op" size="small" class="c-op" @change="onOperatorChange">
      <el-option v-for="o in operatorOptions" :key="o.key" :label="o.label" :value="o.key" />
    </el-select>

    <!-- 版块：多选下拉（IN 语义） -->
    <el-select
      v-if="model.field === 'fid' && (model.op === 'in' || model.op === 'not_in')"
      v-model="model.value"
      size="small"
      class="c-value"
      multiple
      collapse-tags
      collapse-tags-tooltip
      placeholder="选择版块"
    >
      <el-option
        v-for="o in fidOptions"
        :key="o.fid"
        :label="`${o.fid} ${o.name}`"
        :value="o.fid"
      />
    </el-select>

    <!-- 日期区间 -->
    <template v-else-if="arity === 2 && fieldType === 'date'">
      <el-date-picker
        v-model="rangeStart"
        size="small"
        type="date"
        value-format="YYYY-MM-DD"
        placeholder="开始日期"
        class="c-range"
      />
      <span class="c-tilde">~</span>
      <el-date-picker
        v-model="rangeEnd"
        size="small"
        type="date"
        value-format="YYYY-MM-DD"
        placeholder="结束日期"
        class="c-range"
      />
    </template>

    <!-- 数值区间 -->
    <template v-else-if="arity === 2">
      <el-input v-model="rangeStart" size="small" class="c-range" placeholder="最小值" />
      <span class="c-tilde">~</span>
      <el-input v-model="rangeEnd" size="small" class="c-range" placeholder="最大值" />
    </template>

    <!-- 多值 -->
    <el-input
      v-else-if="arity === -1"
      v-model="multiValue"
      size="small"
      class="c-value"
      placeholder="多个值用逗号分隔"
    />

    <!-- 无需值 -->
    <span v-else-if="arity === 0" class="c-none">—</span>

    <!-- 单值：日期用日期选择器，数字用 number -->
    <el-date-picker
      v-else-if="fieldType === 'date'"
      v-model="model.value"
      size="small"
      type="date"
      value-format="YYYY-MM-DD"
      class="c-value"
    />
    <el-input
      v-else-if="fieldType === 'number'"
      v-model="model.value"
      size="small"
      class="c-value"
      placeholder="数值"
    />
    <el-input v-else v-model="model.value" size="small" class="c-value" placeholder="值" />

    <el-button size="small" text type="danger" @click="emit('remove')">✕</el-button>
  </div>
</template>

<style scoped>
.condition-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.c-field {
  width: 104px;
}

.c-op {
  width: 148px;
}

.c-value {
  flex: 1;
  min-width: 120px;
}

.c-range {
  width: 138px;
}

.c-tilde {
  color: #909399;
}

.c-none {
  color: #c0c4cc;
  padding: 0 4px;
}
</style>
