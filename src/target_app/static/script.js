"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const search = document.getElementById("member_id_search");
  if (search instanceof HTMLInputElement) {
    search.addEventListener("input", () => {
      search.setAttribute("aria-invalid", search.value !== "" && !/^[0-9]+$/.test(search.value) ? "true" : "false");
    });
  }
});
