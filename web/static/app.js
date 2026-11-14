// Small behaviours on top of HTMX. No build step, no dependencies.
(function () {
  "use strict";
  var modal = document.getElementById("modal");
  var toast = document.getElementById("toast");
  var toastTimer = null;

  function closeModal() {
    if (modal && modal.open) modal.close();
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (target.closest("[data-modal-close]")) closeModal();
    if (target === modal) closeModal(); // click on the backdrop
    var dismiss = target.closest("[data-dismiss]");
    if (dismiss) dismiss.closest(".toast").remove();

    var all = target.closest("[data-check-all]");
    if (all) {
      var on = all.getAttribute("data-check-all") === "true";
      all.closest("form").querySelectorAll('input[type="checkbox"][name="pick"]').forEach(function (box) {
        box.checked = on;
      });
    }

    if (target.id === "reset-prompt") {
      document.getElementById("system_prompt").value = document.getElementById("default-prompt").value;
      document.getElementById("reset-note").textContent = "Default prompt filled in — press Save settings to keep it.";
    }
  });

  // The recipe picker loads into the dialog; open it once its content arrives.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target && target.id === "modal-body" && modal && !modal.open) {
      modal.showModal();
      var search = modal.querySelector('input[type="search"]');
      if (search && window.matchMedia("(min-width: 700px)").matches) search.focus();
    }
    if (target && target.id === "toast") {
      window.clearTimeout(toastTimer);
      toastTimer = window.setTimeout(function () { toast.innerHTML = ""; }, 12000);
    }
  });

  // A toast can't be seen behind an open dialog, so close the dialog first.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.target && event.detail.target.id === "toast") closeModal();
  });

  // Choosing a recipe in the picker closes it.
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var source = event.detail.elt;
    if (event.detail.successful && source && source.closest && source.closest("[data-modal-close-on-success]")) {
      closeModal();
    }
  });

  // Network failures and unexpected statuses: say so instead of doing nothing.
  function showToast(message) {
    if (!toast) return;
    closeModal();
    toast.innerHTML = "";
    var box = document.createElement("div");
    box.className = "toast toast-error";
    box.setAttribute("role", "alert");
    var text = document.createElement("span");
    text.textContent = message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "iconbtn";
    close.setAttribute("data-dismiss", "");
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    box.appendChild(text);
    box.appendChild(close);
    toast.appendChild(box);
  }
  document.body.addEventListener("htmx:sendError", function () {
    showToast("Could not reach the planner app. Check your connection and try again.");
  });
  document.body.addEventListener("htmx:responseError", function (event) {
    var status = event.detail.xhr ? event.detail.xhr.status : 0;
    if (status === 401) return; // the login redirect handles it
    showToast("That didn't work (error " + status + "). Reload the page and try again.");
  });

  // Ordinary form posts that take a while: show that something is happening.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-busy]")) return;
    var button = form.querySelector('button[type="submit"]');
    if (!button) return;
    window.setTimeout(function () {
      button.classList.add("is-busy");
      button.disabled = true;
    }, 0);
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button.is-busy").forEach(function (button) {
      button.classList.remove("is-busy");
      button.disabled = false;
    });
  });
})();
