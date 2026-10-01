/* VClare arbitration console.
 *
 * Renders the two VClare confirmation questions and records the engineer's
 * answer. The console is served by webui/server.py, which builds the questions
 * with the same Python objects used by the pipeline.
 */

(function () {
  "use strict";

  var state = {
    questions: [],
    decisions: [],
    activeId: null,
    offline: false,
  };

  // Minimal fallback so the page is still inspectable when opened as a file.
  var FALLBACK = {
    questions: [
      {
        question_id: "offline-spec",
        kind: "inconsistency_pair",
        task_id: "demo_counter_contradiction",
        defect_type: "contradictory",
        prompt: "Inconsistency pair 1 of 2: which statement reflects the intended behavior?",
        options: [
          { value: "source1", label: "Source 1 is correct", detail: "reset is active-high and synchronous." },
          { value: "source2", label: "Source 2 is correct", detail: "reset is active-low and asynchronous." },
          { value: "irrelevant", label: "Irrelevant, discard this pair", detail: "" }
        ],
        context: { index: 1, total_pairs: 2, rationale: "The two clauses disagree on polarity and timing." }
      },
      {
        question_id: "offline-sim",
        kind: "behavior_choice",
        task_id: "demo_arbiter_tiebreak",
        defect_type: "incomplete",
        prompt: "The two highest-ranked behavioral clusters disagree on test case 't3'. Which expected output is intended?",
        options: [
          { value: "cluster_1", label: "Cluster 1 behavior is correct", detail: "t3 -> 0001" },
          { value: "cluster_2", label: "Cluster 2 behavior is correct", detail: "t3 -> 0010" },
          { value: "abstain", label: "No confirmation available", detail: "" }
        ],
        context: {
          test_case: "t3",
          clusters: [
            { cluster_id: 0, size: 4, score: 4, source: "// cluster 1: lowest-index priority", outputs: { t3: "0001" } },
            { cluster_id: 1, size: 2, score: 2, source: "// cluster 2: highest-index priority", outputs: { t3: "0010" } }
          ]
        }
      }
    ]
  };

  var els = {};

  document.addEventListener("DOMContentLoaded", function () {
    els.queue = document.getElementById("queue-list");
    els.queueCount = document.getElementById("queue-count");
    els.detail = document.getElementById("detail");
    els.log = document.getElementById("log-list");
    els.logCount = document.getElementById("log-count");
    els.logEmpty = document.getElementById("log-empty");
    els.pending = document.getElementById("pending-pill");
    els.clear = document.getElementById("btn-clear");

    els.clear.addEventListener("click", clearLog);
    load();
  });

  // ------------------------------------------------------------------ loading
  function load() {
    fetch("/api/questions", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.questions = data.questions || [];
        state.decisions = data.decisions || [];
        state.offline = false;
        render();
      })
      .catch(function () {
        state.questions = FALLBACK.questions;
        state.decisions = readLocal();
        state.offline = true;
        render();
      });
  }

  function readLocal() {
    try {
      return JSON.parse(localStorage.getItem("vclare.decisions") || "[]");
    } catch (error) {
      return [];
    }
  }

  function writeLocal(decisions) {
    try {
      localStorage.setItem("vclare.decisions", JSON.stringify(decisions));
    } catch (error) {
      /* storage unavailable, keep in-memory only */
    }
  }

  // ----------------------------------------------------------------- decision
  function decide(question, value) {
    var option = question.options.filter(function (item) {
      return item.value === value;
    })[0];
    if (!option) return;

    var record = {
      question_id: question.question_id,
      kind: question.kind,
      task_id: question.task_id,
      value: value,
      label: option.label,
      defect_type: question.defect_type || "",
      source: state.offline ? "local" : "human",
      timestamp: Date.now() / 1000,
    };

    if (state.offline) {
      upsertDecision(record);
      state.decisions = readLocal();
      render();
      return;
    }

    fetch("/api/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: question.question_id, value: value })
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function () {
        return fetch("/api/decisions", { cache: "no-store" }).then(function (res) {
          return res.json();
        });
      })
      .then(function (data) {
        state.decisions = data.decisions || [];
        render();
      })
      .catch(function () {
        upsertDecision(record);
        state.decisions = readLocal();
        state.offline = true;
        render();
      });
  }

  function upsertDecision(record) {
    var decisions = readLocal().filter(function (item) {
      return item.question_id !== record.question_id;
    });
    decisions.push(record);
    writeLocal(decisions);
  }

  function decisionFor(questionId) {
    return state.decisions.filter(function (item) {
      return item.question_id === questionId;
    })[0];
  }

  function clearLog() {
    if (state.offline) {
      writeLocal([]);
      state.decisions = [];
      render();
      return;
    }
    fetch("/api/clear", { method: "POST" }).then(function () {
      state.decisions = [];
      render();
    });
  }

  // -------------------------------------------------------------------- render
  function render() {
    var resolved = state.decisions.length;
    els.pending.textContent = Math.max(state.questions.length - resolved, 0) + " pending";
    els.queueCount.textContent = state.questions.length + " items";
    els.logCount.textContent = state.decisions.length + " entries";
    els.logEmpty.style.display = state.decisions.length ? "none" : "block";

    if (!state.activeId && state.questions.length) {
      var firstOpen = state.questions.filter(function (q) {
        return !decisionFor(q.question_id);
      })[0];
      state.activeId = (firstOpen || state.questions[0]).question_id;
    }

    renderQueue();
    renderDetail();
    renderLog();
  }

  function renderQueue() {
    els.queue.innerHTML = "";
    state.questions.forEach(function (question) {
      var decision = decisionFor(question.question_id);
      var item = document.createElement("li");
      item.className = "queue-item";
      if (question.question_id === state.activeId) item.classList.add("is-active");
      if (decision) item.classList.add("is-done");
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");

      var marker = document.createElement("span");
      marker.className = "marker " + (question.kind === "inconsistency_pair" ? "spec" : "sim");

      var body = document.createElement("span");
      body.className = "qi-body";
      var title = document.createElement("span");
      title.className = "qi-title";
      title.textContent = question.task_id;
      var sub = document.createElement("span");
      sub.className = "qi-sub";
      sub.textContent =
        (question.kind === "inconsistency_pair" ? "Spec-Level" : "Sim-Level") +
        (question.defect_type ? " · " + question.defect_type : "");
      body.appendChild(title);
      body.appendChild(sub);

      var status = document.createElement("span");
      status.className = "qi-state";
      status.textContent = decision ? "resolved" : "pending";

      item.appendChild(marker);
      item.appendChild(body);
      item.appendChild(status);
      item.addEventListener("click", function () {
        state.activeId = question.question_id;
        render();
      });
      item.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          state.activeId = question.question_id;
          render();
        }
      });
      els.queue.appendChild(item);
    });
  }

  function renderDetail() {
    var question = state.questions.filter(function (item) {
      return item.question_id === state.activeId;
    })[0];

    els.detail.innerHTML = "";
    if (!question) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No confirmation items.";
      els.detail.appendChild(empty);
      return;
    }

    if (question.kind === "inconsistency_pair") {
      renderSpecQuestion(question);
    } else {
      renderBehaviorQuestion(question);
    }
  }

  function renderHead(question, label) {
    var head = document.createElement("div");
    head.className = "detail-head";

    var eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow " + (question.kind === "inconsistency_pair" ? "spec" : "sim");
    eyebrow.textContent = label;

    var title = document.createElement("h1");
    title.textContent = question.task_id;

    var badges = document.createElement("div");
    badges.className = "badges";
    if (question.defect_type) {
      var badge = document.createElement("span");
      badge.className = "badge " + question.defect_type;
      badge.textContent = question.defect_type;
      badges.appendChild(badge);
    }
    head.appendChild(eyebrow);
    head.appendChild(title);
    head.appendChild(badges);
    els.detail.appendChild(head);
    return head;
  }

  function renderSpecQuestion(question) {
    var head = renderHead(question, "Spec-Level Repair");
    var total = (question.context && question.context.total_pairs) || 1;
    var index = (question.context && question.context.index) || 1;
    head.querySelector(".badges").appendChild(makeBadge("pair " + index + " of " + total));

    var body = document.createElement("div");
    body.className = "detail-body";

    var text = document.createElement("p");
    text.className = "question-text";
    text.textContent = question.prompt;
    body.appendChild(text);

    var rationale = question.context && question.context.rationale;
    if (rationale) {
      var note = document.createElement("p");
      note.className = "context-note";
      note.textContent = rationale;
      body.appendChild(note);
    }

    var sources = document.createElement("div");
    sources.className = "sources";
    sources.appendChild(statementCard("Source 1", question.options[0].detail || question.options[0].label));
    sources.appendChild(statementCard("Source 2", question.options[1].detail || question.options[1].label));
    body.appendChild(sources);

    var spec = question.spec;
    if (spec) {
      body.appendChild(specDetails(spec));
    }
    els.detail.appendChild(body);
    renderActions(question);
  }

  function statementCard(label, text) {
    var card = document.createElement("article");
    card.className = "source";
    var head = document.createElement("div");
    head.className = "source-head";
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = label;
    head.appendChild(chip);
    var cardBody = document.createElement("div");
    cardBody.className = "source-body";
    var paragraph = document.createElement("p");
    paragraph.textContent = text;
    cardBody.appendChild(paragraph);
    card.appendChild(head);
    card.appendChild(cardBody);
    return card;
  }

  function renderBehaviorQuestion(question) {
    var head = renderHead(question, "Sim-Level Repair");

    var context = question.context || {};
    var clusters = context.clusters || [];
    var details = question.test_case_details || {};
    var distinguishing = context.distinguishing || {};
    var testCase = context.test_case || distinguishing.test_case;
    head.querySelector(".badges").appendChild(
      makeBadge(clusters.length + " clusters")
    );

    var body = document.createElement("div");
    body.className = "detail-body";

    var text = document.createElement("p");
    text.className = "question-text";
    text.textContent = question.prompt;
    body.appendChild(text);

    if (testCase && details[testCase]) {
      var note = document.createElement("p");
      note.className = "context-note";
      note.textContent =
        "Test case " + testCase + " stimuli: " + details[testCase].inputs +
        (details[testCase].note ? " · " + details[testCase].note : "");
      body.appendChild(note);
    }

    if (clusters.length >= 2) {
      body.appendChild(comparisonTable(clusters, details, testCase));
    }

    var sources = document.createElement("div");
    sources.className = "sources";
    clusters.slice(0, 2).forEach(function (cluster, index) {
      sources.appendChild(clusterCard(cluster, index));
    });
    body.appendChild(sources);

    if (question.spec) {
      body.appendChild(specDetails(question.spec));
    }
    els.detail.appendChild(body);
    renderActions(question);
  }

  function comparisonTable(clusters, details, testCase) {
    var first = clusters[0];
    var second = clusters[1];
    var cases = Object.keys(first.outputs || {});

    var table = document.createElement("table");
    table.className = "io-table";
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Test case", "Stimuli", "Cluster 1 output", "Cluster 2 output"].forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    cases.forEach(function (name) {
      var row = document.createElement("tr");
      var outA = (first.outputs || {})[name];
      var outB = (second.outputs || {})[name];
      if (name === testCase && outA !== outB) row.className = "diff-row";

      var tdName = document.createElement("td");
      tdName.textContent = name;
      var tdStim = document.createElement("td");
      tdStim.textContent = (details[name] && details[name].inputs) || "";
      var tdA = document.createElement("td");
      tdA.className = "mono";
      tdA.textContent = String(outA);
      var tdB = document.createElement("td");
      tdB.className = "mono";
      tdB.textContent = String(outB);

      row.appendChild(tdName);
      row.appendChild(tdStim);
      row.appendChild(tdA);
      row.appendChild(tdB);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    return table;
  }

  function clusterCard(cluster, index) {
    var card = document.createElement("article");
    card.className = "source";

    var head = document.createElement("div");
    head.className = "source-head";
    var chip = document.createElement("span");
    chip.className = "chip sim";
    chip.textContent = "Cluster " + (index + 1);
    var score = document.createElement("span");
    score.className = "muted";
    score.textContent = "size " + cluster.size + " · MBR " + cluster.score;
    head.appendChild(chip);
    head.appendChild(score);

    var body = document.createElement("div");
    body.className = "source-body";
    var paragraph = document.createElement("p");
    paragraph.textContent = "Representative candidate: " + cluster.representative;
    body.appendChild(paragraph);

    card.appendChild(head);
    card.appendChild(body);

    var pre = document.createElement("pre");
    pre.className = "code";
    pre.innerHTML = highlight(cluster.source || "");
    card.appendChild(pre);
    return card;
  }

  function specDetails(spec) {
    var details = document.createElement("details");
    var summary = document.createElement("summary");
    summary.textContent = "Original specification";
    var pre = document.createElement("pre");
    pre.className = "code";
    pre.textContent = spec;
    details.appendChild(summary);
    details.appendChild(pre);
    return details;
  }

  function renderActions(question) {
    var decision = decisionFor(question.question_id);
    if (decision) {
      var resolved = document.createElement("div");
      resolved.className = "resolved";
      resolved.innerHTML =
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
      var span = document.createElement("span");
      span.textContent = "Recorded: " + decision.label;
      resolved.appendChild(span);
      els.detail.appendChild(resolved);
      return;
    }

    var actions = document.createElement("div");
    actions.className = "actions";
    question.options.forEach(function (option) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn-choice" + (option.value === "irrelevant" || option.value === "abstain" ? " discard" : "");
      var label = document.createElement("span");
      label.className = "bc-label";
      label.textContent = option.label;
      button.appendChild(label);
      if (option.detail) {
        var sub = document.createElement("span");
        sub.className = "bc-sub";
        sub.textContent = option.detail;
        button.appendChild(sub);
      }
      button.addEventListener("click", function () {
        decide(question, option.value);
      });
      actions.appendChild(button);
    });
    els.detail.appendChild(actions);
  }

  function makeBadge(text) {
    var badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = text;
    return badge;
  }

  function renderLog() {
    els.log.innerHTML = "";
    state.decisions
      .slice()
      .sort(function (a, b) {
        return (b.timestamp || 0) - (a.timestamp || 0);
      })
      .forEach(function (decision) {
        var item = document.createElement("li");
        item.className = "log-item " + (decision.kind === "inconsistency_pair" ? "spec" : "sim");
        var top = document.createElement("div");
        top.className = "li-top";
        var task = document.createElement("span");
        task.className = "li-task";
        task.textContent = decision.task_id;
        var time = document.createElement("span");
        time.textContent = decision.source === "auto" ? "auto" : "human";
        top.appendChild(task);
        top.appendChild(time);
        var choice = document.createElement("div");
        choice.className = "li-choice";
        choice.textContent = decision.label || decision.value;
        item.appendChild(top);
        item.appendChild(choice);
        els.log.appendChild(item);
      });
  }

  // ------------------------------------------------------------- highlighting
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  var KEYWORDS =
    "module|endmodule|input|output|inout|logic|wire|reg|always|assign|if|else|" +
    "begin|end|case|casez|casex|default|endcase|posedge|negedge|parameter|" +
    "localparam|initial|for|while|generate|endgenerate|function|endfunction";

  function highlight(source) {
    var text = escapeHtml(source);
    var pattern = new RegExp(
      "(\\/\\/[^\\n]*)|(\\b\\d+'[bodhBODH][0-9a-fA-FxzXZ_]+)|(\"(?:[^\"\\\\]|\\\\.)*\")|\\b(" +
        KEYWORDS +
        ")\\b",
      "g"
    );
    return text.replace(pattern, function (match, comment, number, string, keyword) {
      if (comment) return '<span class="tok-com">' + comment + "</span>";
      if (number) return '<span class="tok-num">' + number + "</span>";
      if (string) return '<span class="tok-str">' + string + "</span>";
      if (keyword) return '<span class="tok-key">' + keyword + "</span>";
      return match;
    });
  }
})();
