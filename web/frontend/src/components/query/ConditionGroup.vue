<script setup lang="ts">
import { emptyCondition, emptyGroup, isGroup, type GroupNode } from '../../utils/queryMeta'
import ConditionRow from './ConditionRow.vue'
// 递归自引用：模板里嵌套渲染自身，必须显式导入（SFC 文件名即组件名）
import ConditionGroup from './ConditionGroup.vue'

/** 条件分组（递归组件）：组内条件用 AND / OR 连接，可再嵌套一组（后端上限 3 层） */
const props = defineProps<{
  node: GroupNode
  depth: number
  fidOptions: { fid: string; name: string }[]
  removable: boolean
}>()
const emit = defineEmits<{ remove: [] }>()

const MAX_DEPTH = 3

function addCondition() {
  props.node.rules.push(emptyCondition())
}

function addGroup() {
  props.node.rules.push(emptyGroup())
}

function removeAt(index: number) {
  props.node.rules.splice(index, 1)
}
</script>

<template>
  <div class="group" :class="{ 'group-nested': depth > 1 }">
    <div class="group-head">
      <el-radio-group v-model="node.op" size="small">
        <el-radio-button value="AND">且 AND</el-radio-button>
        <el-radio-button value="OR">或 OR</el-radio-button>
      </el-radio-group>
      <span class="group-hint">组内条件按此连接</span>
      <div class="group-actions">
        <el-button size="small" @click="addCondition">+ 条件</el-button>
        <el-button v-if="depth < MAX_DEPTH" size="small" @click="addGroup">+ 分组</el-button>
        <el-button v-if="removable" size="small" text type="danger" @click="emit('remove')">
          删除分组
        </el-button>
      </div>
    </div>

    <div v-for="(child, i) in node.rules" :key="i" class="group-body">
      <ConditionGroup
        v-if="isGroup(child)"
        :node="child"
        :depth="depth + 1"
        :fid-options="fidOptions"
        removable
        @remove="removeAt(i)"
      />
      <ConditionRow v-else :model="child" :fid-options="fidOptions" @remove="removeAt(i)" />
    </div>

    <div v-if="!node.rules.length" class="group-empty">该分组暂无条件</div>
  </div>
</template>

<style scoped>
.group {
  border: 1px dashed #dcdfe6;
  border-radius: 6px;
  padding: 8px 10px;
}

.group-nested {
  background: #fafafa;
  margin-bottom: 6px;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.group-hint {
  color: #909399;
  font-size: 12px;
}

.group-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.group-empty {
  color: #c0c4cc;
  font-size: 12px;
  padding: 4px 0;
}
</style>
