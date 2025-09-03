(async function () {
  const res = await fetch('/api/token');
  const { token } = await res.json();
  const dl = new BotFrameworkDirectLine.DirectLine({ token });

  const messages = document.getElementById('messages');
  const input = document.getElementById('input');
  const fileInput = document.getElementById('file-input');
  const sendBtn = document.getElementById('send');

  function appendMessage(text, from) {
    const div = document.createElement('div');
    div.className = `message ${from}`;
    div.textContent = text;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  dl.activity$.subscribe(activity => {
    if (activity.type === 'message' && activity.from && activity.from.id !== 'user') {
      appendMessage(activity.text, 'bot');
    }
  });

  sendBtn.addEventListener('click', async () => {
    const text = input.value.trim();
    if (fileInput.files.length > 0) {
      const file = fileInput.files[0];
      const sasRes = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`);
      const { uploadUrl, blobUrl } = await sasRes.json();
      await fetch(uploadUrl, {
        method: 'PUT',
        headers: { 'x-ms-blob-type': 'BlockBlob' },
        body: file
      });
      await dl.postActivity({
        from: { id: 'user' },
        type: 'event',
        name: 'files_uploaded',
        value: { url: blobUrl, name: file.name }
      }).toPromise();
      fileInput.value = '';
    } else if (text) {
      appendMessage(text, 'user');
      await dl.postActivity({
        from: { id: 'user' },
        type: 'message',
        text
      }).toPromise();
      input.value = '';
    }
  });
})();
