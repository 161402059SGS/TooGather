/* The small amount of behaviour the interface needs.

   A file rather than inline handlers, because the Content-Security-Policy
   allows scripts only from this origin and an inline onclick or onfocus is
   blocked. Everything here is an improvement on a page that already works
   without it: the checkboxes, the buttons and the URL field all function on
   their own if this file never loads. */
document.addEventListener("DOMContentLoaded", function () {

  /* Review queue: one tick to select the whole page of suggestions. */
  var all = document.getElementById("pick-all");
  if (all) {
    var form = document.getElementById("bulk");
    all.addEventListener("change", function () {
      var boxes = form.querySelectorAll('input[name="event_ids"]');
      for (var i = 0; i < boxes.length; i++) boxes[i].checked = all.checked;
    });
  }

  /* A webhook URL exists to be copied, so focusing it selects the whole
     thing rather than dropping a caret in the middle. */
  var urls = document.querySelectorAll(".hook-url");
  for (var j = 0; j < urls.length; j++) {
    urls[j].addEventListener("focus", function () { this.select(); });
  }
});
