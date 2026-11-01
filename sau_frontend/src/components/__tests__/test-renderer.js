import { createRenderer, h, nextTick } from 'vue'

// A tiny in-memory Vue host: exercises real SFC setup/render/lifecycle without a
// browser, Playwright, jsdom or any network. Element Plus shells are slot stubs.
const node = (type, text = '') => ({ type, text, children: [], props: {}, style: {}, parent: null })
function remove(child) {
  if (!child.parent) return
  const siblings = child.parent.children
  const index = siblings.indexOf(child)
  if (index >= 0) siblings.splice(index, 1)
  child.parent = null
}
function insert(child, parent, anchor = null) {
  remove(child)
  const index = anchor ? parent.children.indexOf(anchor) : -1
  parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  child.parent = parent
}
const renderer = createRenderer({
  createElement: (type) => node(type),
  createText: (text) => node('text', text),
  createComment: (text) => node('comment', text),
  setText: (element, text) => { element.text = text },
  setElementText: (element, text) => { element.text = text; element.children = [] },
  patchProp: (element, key, _previous, value) => { element.props[key] = value },
  insert, remove,
  parentNode: (element) => element.parent,
  nextSibling: (element) => element.parent?.children[element.parent.children.indexOf(element) + 1] || null,
  setScopeId: () => {},
  insertStaticContent: (content, parent, anchor) => {
    const element = node('static', content)
    insert(element, parent, anchor)
    return [element, element]
  },
})
const shells = ['card', 'tag', 'button', 'progress', 'alert', 'tooltip', 'select', 'option', 'upload',
  'icon', 'input', 'input-number', 'switch', 'checkbox', 'checkbox-group', 'radio', 'radio-group',
  'radio-button', 'collapse', 'collapse-item', 'divider', 'form', 'form-item', 'dialog', 'table',
  'table-column', 'date-picker', 'empty', 'row', 'col', 'space', 'image', 'drawer', 'link', 'text']

export function mount(component, props = {}) {
  const root = node('root')
  const app = renderer.createApp(component, props)
  for (const name of [...shells.map((name) => `el-${name}`), 'router-link']) {
    app.component(name, { inheritAttrs: false, setup: (_, { slots, attrs }) => () => h(name, attrs, [
      name === 'el-alert' ? attrs.title : '', slots.header?.(), slots.default?.(), slots.footer?.(),
    ]) })
  }
  app.directive('loading', {})
  const vm = app.mount(root)
  return { root, state: vm.$.setupState, unmount: () => app.unmount(), vm }
}

export function textContent(element) {
  return element.type === 'comment' ? '' : `${element.text}${element.children.map(textContent).join('')}`
}
export function findNodes(element, predicate) {
  return [...(predicate(element) ? [element] : []), ...element.children.flatMap((child) => findNodes(child, predicate))]
}
export async function flush() {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await nextTick()
}
