/** 高级查询的条件树结构与字段/操作符元数据（与后端 query_builder.py 白名单一一对应）。

 *  后端是唯一权威：这里只定义「可选什么」，合法性由后端最终校验。
 */
export interface ConditionNode {
  field: string
  op: string
  /** 值：between 为 [起始, 结束]，in/not_in 为数组，其余为字符串 */
  value: string | string[]
}

export interface GroupNode {
  op: 'AND' | 'OR'
  rules: QueryNode[]
}

export type QueryNode = ConditionNode | GroupNode

export function isGroup(node: QueryNode): node is GroupNode {
  return (node as GroupNode).rules !== undefined
}

export type FieldType = 'text' | 'number' | 'date'

export interface FieldMeta {
  key: string
  label: string
  type: FieldType
}

/** 字段列表（与后端 FIELDS 白名单一致；顺序即下拉顺序） */
export const QUERY_FIELDS: FieldMeta[] = [
  { key: 'fid', label: '版块', type: 'text' },
  { key: 'title', label: '标题', type: 'text' },
  { key: 'author', label: '作者', type: 'text' },
  { key: 'likes', label: '点赞', type: 'number' },
  { key: 'replies', label: '回复', type: 'number' },
  { key: 'date', label: '发布日期', type: 'date' },
  { key: 'update_date', label: '入库日期', type: 'date' },
]

export interface OperatorMeta {
  key: string
  label: string
  /** 值的个数：0 无需值，1 单值，2 两个值（区间），-1 多值（IN） */
  arity: number
}

const TEXT_OPS: OperatorMeta[] = [
  { key: 'contains', label: '包含', arity: 1 },
  { key: 'not_contains', label: '不包含', arity: 1 },
  { key: 'eq', label: '等于', arity: 1 },
  { key: 'ne', label: '不等于', arity: 1 },
  { key: 'starts_with', label: '开头是', arity: 1 },
  { key: 'in', label: '属于（多个用逗号分隔）', arity: -1 },
  { key: 'not_in', label: '不属于', arity: -1 },
  { key: 'is_empty', label: '为空', arity: 0 },
  { key: 'is_not_empty', label: '不为空', arity: 0 },
]

const NUMBER_OPS: OperatorMeta[] = [
  { key: 'gt', label: '大于', arity: 1 },
  { key: 'gte', label: '大于等于', arity: 1 },
  { key: 'lt', label: '小于', arity: 1 },
  { key: 'lte', label: '小于等于', arity: 1 },
  { key: 'eq', label: '等于', arity: 1 },
  { key: 'ne', label: '不等于', arity: 1 },
  { key: 'between', label: '介于（含端点）', arity: 2 },
]

const DATE_OPS: OperatorMeta[] = [
  { key: 'between', label: '介于（含端点）', arity: 2 },
  { key: 'gte', label: '不早于', arity: 1 },
  { key: 'lte', label: '不晚于', arity: 1 },
  { key: 'eq', label: '等于', arity: 1 },
  { key: 'ne', label: '不等于', arity: 1 },
]

export const OPERATORS_BY_TYPE: Record<FieldType, OperatorMeta[]> = {
  text: TEXT_OPS,
  number: NUMBER_OPS,
  date: DATE_OPS,
}

export function fieldMeta(key: string): FieldMeta | undefined {
  return QUERY_FIELDS.find((f) => f.key === key)
}

export function operatorsOf(field: string): OperatorMeta[] {
  const meta = fieldMeta(field)
  return meta ? OPERATORS_BY_TYPE[meta.type] : TEXT_OPS
}

/** 切换字段后，若当前操作符对新字段不适用则回落到第一个可用操作符 */
export function normalizeOperator(field: string, op: string): string {
  const ops = operatorsOf(field)
  return ops.some((o) => o.key === op) ? op : ops[0].key
}

export function emptyCondition(): ConditionNode {
  return { field: 'title', op: 'contains', value: '' }
}

export function emptyGroup(): GroupNode {
  return { op: 'OR', rules: [emptyCondition(), emptyCondition()] }
}

/** 条件树的简要描述（供筛选摘要条展示） */
export function describeNode(node: QueryNode): string {
  if (!isGroup(node)) {
    const f = fieldMeta(node.field)?.label ?? node.field
    const op = operatorsOf(node.field).find((o) => o.key === node.op)?.label ?? node.op
    const v = Array.isArray(node.value) ? node.value.join(' ~ ') : node.value
    return v ? `${f} ${op} ${v}` : `${f} ${op}`
  }
  const joiner = node.op === 'AND' ? ' 且 ' : ' 或 '
  const inner = node.rules.map(describeNode).join(joiner)
  return node.rules.length > 1 ? `(${inner})` : inner
}
