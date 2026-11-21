function setupContactDelivery(request, segment) {
    const fieldset = document.getElementById('eventHubSpotOutbound');
    const subscription = document.getElementById('eventHubSpotSubscription');
    const load = document.getElementById('eventHubSpotLoadSubscriptions');
    const enable = document.getElementById('eventHubSpotEnableOutbound');
    const disable = document.getElementById('eventHubSpotDisableOutbound');
    const read = document.getElementById('eventHubSpotReadOutbound');
    const status = document.getElementById('eventHubSpotOutboundStatus');
    const engagementSettings = document.getElementById('eventHubSpotEngagementSettings');
    const engagementEvent = document.getElementById('eventHubSpotEngagementEvent');
    let base = '';
    let generation = 0;
    const inFlight = new Set();
    const setStatus = (text) => {
        status.textContent = text;
    };
    const call = async (b, gen, path, method, body, integration) => {
        if (!b || b !== base || gen !== generation)
            return;
        if (inFlight.has(gen))
            return;
        inFlight.add(gen);
        try {
            const result = await request(path, method, body, integration);
            return b === base && gen === generation ? result : undefined;
        }
        catch (error) {
            if (b === base && gen === generation) {
                setStatus(error instanceof Error ? error.message : 'Request failed');
            }
            return undefined;
        }
        finally {
            inFlight.delete(gen);
        }
    };
    const refresh = async (b, gen) => {
        const receipt = await call(b, gen, `${b}/registration/hubspot/outbound`, 'GET');
        if (!receipt)
            return;
        if (receipt.available === false) {
            fieldset.hidden = true;
            engagementSettings.hidden = true;
            engagementEvent.disabled = true;
            engagementEvent.value = '';
            setStatus('Outbound unavailable');
            return;
        }
        fieldset.hidden = false;
        if (receipt.engagement_available === false) {
            engagementSettings.hidden = true;
            engagementEvent.disabled = true;
            engagementEvent.value = '';
        }
        else {
            engagementSettings.hidden = false;
            engagementEvent.disabled = false;
            engagementEvent.value = receipt.engagement_event ?? '';
        }
        const keys = ['queued', 'waiting', 'sent', 'skipped', 'canceled', 'problem'];
        const counts = keys.map((key) => `${key}: ${receipt.states?.[key] ?? 0}`).join(', ');
        const enabledText = receipt.enabled
            ? 'Registration delivery enabled'
            : 'Registration delivery is off';
        const ids = [
            receipt.segment ? `segment ${receipt.segment}` : '',
            receipt.subscription ? `subscription ${receipt.subscription}` : ''
        ].filter(Boolean).join(', ');
        const registrationStatus = [enabledText, counts, ids].filter(Boolean).join(' · ');
        const statusParts = [];
        if (receipt.engagement_event) {
            const eKeys = ['queued', 'waiting', 'accepted', 'skipped', 'canceled', 'problem'];
            const eCounts = eKeys.map((key) => `${key}: ${receipt.engagement_states?.[key] ?? 0}`).join(', ');
            statusParts.push(`Engagement event ${receipt.engagement_event} · ${eCounts}`);
        }
        statusParts.push(registrationStatus);
        setStatus(statusParts.join(' · '));
    };
    load.addEventListener('click', async () => {
        if (!base)
            return;
        const b = base;
        const gen = generation;
        const data = await call(b, gen, '/api/integrations/hubspot/subscriptions', 'GET');
        if (!data)
            return;
        subscription.replaceChildren(new Option("Choose an email subscription", ""));
        subscription.value = '';
        for (const item of data.subscriptions ?? []) {
            subscription.append(new Option(item.name, item.id));
        }
    });
    enable.addEventListener('click', async () => {
        if (!base)
            return;
        if (!segment.value || !subscription.value) {
            setStatus('Select a segment and a subscription');
            return;
        }
        const b = base;
        const gen = generation;
        const result = await call(b, gen, `${b}/registration/hubspot/outbound`, 'PUT', {
            segment: segment.value,
            subscription: subscription.value,
            engagement_event: engagementEvent.disabled ? '' : engagementEvent.value.trim()
        }, true);
        if (result)
            void refresh(b, gen);
    });
    disable.addEventListener('click', async () => {
        if (!base)
            return;
        const b = base;
        const gen = generation;
        const result = await call(b, gen, `${b}/registration/hubspot/outbound`, 'DELETE', {}, true);
        if (result && b === base && gen === generation)
            void refresh(b, gen);
    });
    read.addEventListener('click', () => {
        if (!base)
            return;
        void refresh(base, generation);
    });
    return (newBase) => {
        base = newBase;
        generation += 1;
        subscription.replaceChildren(new Option("Choose an email subscription", ""));
        subscription.value = '';
        engagementSettings.hidden = true;
        engagementEvent.disabled = true;
        engagementEvent.value = '';
        setStatus('');
        fieldset.hidden = true;
        if (base)
            void refresh(base, generation);
    };
}
export function setupHubSpot(request, refreshRoster) {
    const listsBtn = document.querySelector("#eventHubSpotLists");
    const moreBtn = document.querySelector("#eventHubSpotMore");
    const segmentSel = document.querySelector("#eventHubSpotSegment");
    const importBtn = document.querySelector("#eventHubSpotImport");
    const status = document.querySelector("#eventHubSpotStatus");
    const startSyncBtn = document.querySelector("#eventHubSpotStartSync");
    const stopSyncBtn = document.querySelector("#eventHubSpotStopSync");
    const readSyncBtn = document.querySelector("#eventHubSpotReadSync");
    const syncStatus = document.querySelector("#eventHubSpotSyncStatus");
    const chooseOutbound = setupContactDelivery(request, segmentSel);
    let base = "";
    let scope = 0;
    let cursor = null;
    let busy = false;
    const setBusy = (v) => {
        busy = v;
        listsBtn.disabled = v;
        moreBtn.disabled = v;
        segmentSel.disabled = v;
        importBtn.disabled = v || !segmentSel.value;
        startSyncBtn.disabled = v || !segmentSel.value;
        stopSyncBtn.disabled = v;
        readSyncBtn.disabled = v;
    };
    segmentSel.onchange = () => {
        importBtn.disabled = busy || !segmentSel.value;
        startSyncBtn.disabled = busy || !segmentSel.value;
    };
    const setStatus = (msg) => {
        status.textContent = msg;
    };
    const resetOptions = () => {
        while (segmentSel.options.length > 1)
            segmentSel.remove(1);
        segmentSel.selectedIndex = 0;
    };
    const appendSegments = (segments) => {
        const seen = new Set();
        for (let i = 0; i < segmentSel.options.length; i++)
            seen.add(segmentSel.options[i].value);
        for (const s of segments) {
            if (seen.has(s.id))
                continue;
            seen.add(s.id);
            segmentSel.add(new Option(s.name, s.id));
        }
    };
    const updateMore = () => {
        moreBtn.hidden = !cursor;
    };
    const load = async (after) => {
        if (busy)
            return;
        const gen = scope;
        setBusy(true);
        setStatus(after ? "Loading more…" : "Loading…");
        try {
            const q = after ? `?after=${encodeURIComponent(after)}` : "";
            const res = await request(`/api/integrations/hubspot/segments${q}`);
            if (gen !== scope)
                return;
            if (!after)
                resetOptions();
            appendSegments(res.segments);
            cursor = res.after === after ? null : res.after;
            updateMore();
            setStatus("");
        }
        catch (error) {
            if (gen !== scope)
                return;
            setStatus(error.message);
        }
        finally {
            if (gen === scope)
                setBusy(false);
        }
    };
    listsBtn.addEventListener("click", () => { void load(null); });
    moreBtn.addEventListener("click", () => { if (cursor)
        void load(cursor); });
    importBtn.addEventListener("click", async () => {
        if (busy)
            return;
        const segment = segmentSel.value;
        if (!segment) {
            setStatus("Select a HubSpot list.");
            return;
        }
        const gen = scope;
        setBusy(true);
        setStatus("Importing…");
        try {
            const res = await request(`${base}/registration/hubspot`, "POST", { segment }, true);
            if (gen !== scope)
                return;
            setStatus(`${res.added} added, ${res.existing} already registered, ${res.duplicates} duplicates, ${res.skipped} skipped.`);
            refreshRoster();
        }
        catch (error) {
            if (gen !== scope)
                return;
            setStatus(error.message);
        }
        finally {
            if (gen === scope)
                setBusy(false);
        }
    });
    const syncRequest = async (method) => {
        if (!base || busy)
            return;
        const gen = scope;
        const segment = segmentSel.value;
        setBusy(true);
        syncStatus.textContent =
            method === "GET" ? "Loading sync…" : method === "PUT" ? "Scheduling sync…" : "Stopping sync…";
        try {
            const receipt = await request(`${base}/registration/hubspot/sync`, method, method === "GET" ? undefined : method === "PUT" ? { segment } : {}, method !== "GET");
            if (gen !== scope)
                return;
            const stateLabels = {
                off: "Automatic refresh is off",
                scheduled: "Automatic refresh scheduled",
                complete: "Automatic refresh complete",
                retry: "Waiting to retry",
                problem: "Automatic refresh needs attention",
            };
            const parts = [stateLabels[receipt.state] ?? receipt.state];
            if (receipt.last_at)
                parts.push(`last ${new Date(receipt.last_at * 1000).toLocaleString()}`);
            if (receipt.next_at)
                parts.push(`next ${new Date(receipt.next_at * 1000).toLocaleString()}`);
            if (receipt.last_result) {
                const result = receipt.last_result;
                parts.push(`${result.added} added, ${result.existing} already registered, ${result.duplicates} duplicates, ${result.skipped} skipped`);
                if (result.removed)
                    parts.push(`${result.removed} filtered out`);
                if (result.restored)
                    parts.push(`${result.restored} restored`);
            }
            if (receipt.problem)
                parts.push(receipt.problem);
            syncStatus.textContent = parts.join(" · ");
            stopSyncBtn.hidden = receipt.state === "off";
            if (method === "GET" && receipt.last_result)
                refreshRoster();
        }
        catch (error) {
            if (gen !== scope)
                return;
            syncStatus.textContent = error.message;
        }
        finally {
            if (gen === scope)
                setBusy(false);
        }
    };
    startSyncBtn.addEventListener("click", () => { void syncRequest("PUT"); });
    stopSyncBtn.addEventListener("click", () => { void syncRequest("DELETE"); });
    readSyncBtn.addEventListener("click", () => { void syncRequest("GET"); });
    updateMore();
    return (newBase) => {
        scope++;
        base = newBase;
        cursor = null;
        resetOptions();
        updateMore();
        setStatus("");
        syncStatus.textContent = "";
        stopSyncBtn.hidden = true;
        setBusy(false);
        void syncRequest("GET");
        chooseOutbound(newBase);
    };
}
