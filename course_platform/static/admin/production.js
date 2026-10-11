const guardedForms = new WeakSet();

export function attachDirtyGuard(form) {
  if (guardedForms.has(form)) return;
  guardedForms.add(form);
  const fields = [...form.querySelectorAll('textarea, input:not([type="hidden"]), select')];
  const initial = fields.map(field => field.value);
  const recovered = form.dataset.unsaved === 'true' && fields.some(field => field.value);
  let submitting = false;
  const dirty = () => recovered || fields.some((field, index) => field.value !== initial[index]);
  form.addEventListener('submit', () => { submitting = true; });
  form.addEventListener('input', () => { submitting = false; });
  window.addEventListener('beforeunload', event => {
    // Native save navigates to a new document. Only that navigation is exempt;
    // the source baseline is never cleared ahead of a successful server save.
    if (submitting) { submitting = false; return; }
    if (dirty()) { event.preventDefault(); event.returnValue = ''; }
  });
}

export function insertMarkdown(editor, before, after) {
  const start = editor.selectionStart;
  const selected = editor.value.slice(start, editor.selectionEnd);
  editor.setRangeText(before + selected + after, start, editor.selectionEnd, 'select');
  editor.focus();
  editor.dispatchEvent(new Event('input', { bubbles: true }));
}

const formats = {
  bold: ['**', '**'], italic: ['*', '*'], heading: ['\n## ', '\n'],
  list: ['\n- ', '\n'], code: ['\n```\n', '\n```\n'], link: ['[', '](https://example.com)'],
  sections: ['## 学习目标\n\n## 讲解\n\n## 案例\n\n## 练习\n\n## 总结\n', ''],
};
document.querySelectorAll('form[data-editor-form]').forEach(attachDirtyGuard);
document.querySelectorAll('.markdown-toolbar').forEach(toolbar => {
  const editor = toolbar.closest('form').querySelector('textarea[name="markdown"]');
  if (!editor) return;
  toolbar.hidden = false;
  toolbar.addEventListener('click', event => {
    const button = event.target.closest('button[data-markdown]');
    if (button && formats[button.dataset.markdown]) insertMarkdown(editor, ...formats[button.dataset.markdown]);
  });
});
