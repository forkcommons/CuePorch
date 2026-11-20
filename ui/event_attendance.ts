const status = document.querySelector("#attendanceStatus")!;
const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);

async function join() {
  if (token) {
    try {
      const response = await fetch(location.pathname, {
        method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({token}),
      });
      if (response.ok) {
        const {watch}: {watch: string} = await response.json();
        location.replace(watch);
        return;
      }
    } catch {
      status.textContent = "CuePorch couldn't open the event. Check your connection and try the original link again.";
      return;
    }
  }
  status.textContent = "This personal link is unavailable. Open the event page to register or contact the host.";
}

void join();
