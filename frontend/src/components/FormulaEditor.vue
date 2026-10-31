<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { formulaExamples, formulaTemplates, insertFormulaTemplate, type FormulaTemplate } from '../utils/formulaEditor'
import { renderRichText } from '../utils/richText'

const props = defineProps<{ modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; finished: [] }>()
const editorOpen = ref(false)
const helpOpen = ref(false)
const mobileTab = ref('edit')
const textarea = ref<HTMLTextAreaElement | null>(null)
const selection = ref({ start: 0, end: 0 })
const composing = ref(false)
const preview = computed(() => renderRichText(props.modelValue))

function rememberSelection() {
  if (textarea.value) {
    selection.value = { start: textarea.value.selectionStart, end: textarea.value.selectionEnd }
  }
}
function openEditor() {
  mobileTab.value = 'edit'
  selection.value = { start: props.modelValue.length, end: props.modelValue.length }
  editorOpen.value = true
}
async function focusEditor() {
  await nextTick()
  textarea.value?.focus()
  textarea.value?.setSelectionRange(selection.value.start, selection.value.end)
}
async function insert(template: FormulaTemplate) {
  if (props.disabled || composing.value) return
  if (!editorOpen.value) openEditor()
  const result = insertFormulaTemplate(props.modelValue, selection.value.start, selection.value.end, template)
  emit('update:modelValue', result.text)
  selection.value = { start: result.selectionStart, end: result.selectionEnd }
  helpOpen.value = false
  mobileTab.value = 'edit'
  await focusEditor()
}
function updateText(event: Event) {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
  rememberSelection()
}
</script>

<template>
  <div class="formula-editor-actions">
    <button type="button" class="ghost-button" :disabled="disabled" @click="openEditor">公式编辑</button>
    <button type="button" class="ghost-button" @click="helpOpen = true">语法说明</button>
  </div>
  <el-dialog v-model="editorOpen" title="Markdown 公式编辑" width="min(1100px, 96vw)" class="formula-editor-dialog" append-to-body :close-on-click-modal="false" @opened="focusEditor" @closed="emit('finished')">
    <div class="formula-editor-toolbar" aria-label="常用公式模板">
      <button v-for="item in formulaTemplates" :key="item.label" type="button" class="ghost-button" :disabled="disabled || composing" @mousedown.prevent @click="insert(item)">{{ item.label }}</button>
      <button type="button" class="ghost-button" @click="rememberSelection(); helpOpen = true">语法说明</button>
    </div>
    <div class="formula-editor-tabs" role="tablist" aria-label="编辑和预览">
      <button id="formula-edit-tab" type="button" role="tab" aria-controls="formula-source-panel" :aria-selected="mobileTab === 'edit'" @click="mobileTab = 'edit'">编辑</button>
      <button id="formula-preview-tab" type="button" role="tab" aria-controls="formula-preview-panel" :aria-selected="mobileTab === 'preview'" @click="rememberSelection(); mobileTab = 'preview'">预览</button>
    </div>
    <div class="formula-editor-columns" :data-active-tab="mobileTab">
      <section id="formula-source-panel" class="formula-editor-source" aria-labelledby="formula-source-label">
        <label id="formula-source-label" for="formula-source">Markdown 源码</label>
        <textarea id="formula-source" ref="textarea" :value="modelValue" :disabled="disabled" spellcheck="false" placeholder="输入文字和公式，例如：我认为 $F = ma$，但不知道如何确定加速度。" @input="updateText" @select="rememberSelection" @click="rememberSelection" @keyup="rememberSelection" @blur="rememberSelection" @compositionstart="composing = true" @compositionend="composing = false; updateText($event)" />
      </section>
      <section id="formula-preview-panel" class="formula-editor-preview" aria-label="渲染预览">
        <div class="formula-editor-panel-label">渲染预览</div>
        <div v-if="modelValue.trim()" class="message-body" v-html="preview"></div>
        <p v-else class="formula-editor-empty">在编辑区输入文字或插入公式，这里会显示渲染效果。</p>
      </section>
    </div>
    <template #footer>
      <span class="formula-editor-note">修改自动保留在当前草稿中，完成后可在输入框发送。</span>
      <button type="button" class="primary-button" @click="editorOpen = false">完成编辑</button>
    </template>
  </el-dialog>
  <el-dialog v-model="helpOpen" title="Markdown 与公式语法说明" width="min(900px, 96vw)" class="formula-help-dialog" append-to-body @closed="editorOpen && focusEditor()">
    <p>文字使用 Markdown，公式内部使用 LaTeX 语法。用 <code>$…$</code> 写行内公式，用 <code>$$…$$</code> 写独立公式；也支持 <code>\(…\)</code> 和 <code>\[…\]</code>。</p>
    <p>在已有公式内插入模板时只插入 LaTeX 内容，不重复添加定界符。嵌套分式时，可选中根号中的 x 再点“分式”，得到 <code>$\sqrt{\frac{a}{b}}$</code>。</p>
    <p>公式定界符应成对闭合，花括号用于分组，例如 <code>x_{12}</code>。命令前输入一个反斜杠；公式请写在代码块之外，三个反引号包裹的代码块不会渲染公式。未闭合或错误公式会保留可读内容，部分未知命令会标红显示。</p>
    <div class="formula-help-examples">
      <article v-for="item in formulaExamples" :key="item.label" class="formula-help-example">
        <div class="formula-help-example-heading"><strong>{{ item.label }}</strong><button type="button" class="ghost-button" :disabled="disabled" @click="insert(item)">插入示例</button></div>
        <pre><code>{{ item.source }}</code></pre>
        <div class="message-body" v-html="renderRichText(item.source)"></div>
      </article>
    </div>
    <template #footer><button type="button" class="primary-button" @click="helpOpen = false">知道了</button></template>
  </el-dialog>
</template>
