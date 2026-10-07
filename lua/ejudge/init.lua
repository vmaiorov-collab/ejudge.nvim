local backend = require("ejudge.backend")

local M = { version = "0.1.0" }

local config = {
  dir = "~/ejudge-contests", -- where contests are saved (same layout as the CLion plugin)
  server = "https://ejudge.algocode.ru/cgi-bin/new-client",
  login = "",
  password = nil, -- better leave it empty and use :EjudgeLogin or $EJUDGE_PASSWORD
  python = nil, -- python3 executable, found automatically by default
  statement_width = 72,
  panel_height = 14,
  keymaps = false, -- set to true for the default <leader>e… mappings
}

local state = {
  problem = nil, -- directory of the problem we work on
  letter = nil,
  states = {}, -- letter -> {solved, verdict}
  summary = nil, -- {solved, total}
  cache = {}, -- contest dir -> {time, states}
  inputs = {}, -- problem dir -> text typed in the :EjudgeRun window
}
local bufs = {}

-- ------------------------------------------------------------------ helpers

local function notify(msg, level)
  vim.notify(msg, level or vim.log.levels.INFO, { title = "Ejudge" })
end

local function say(msg)
  vim.api.nvim_echo({ { "Ejudge: " .. msg } }, false, {})
end

local function creds_path()
  return vim.fn.stdpath("data") .. "/ejudge/credentials.json"
end

local function load_credentials()
  local f = io.open(creds_path(), "r")
  if f then
    local ok, data = pcall(vim.json.decode, f:read("*a"))
    f:close()
    if ok and type(data) == "table" then
      if (config.login or "") == "" then config.login = data.login or "" end
      if not config.password or config.password == "" then config.password = data.password end
    end
  end
  if (not config.password or config.password == "") and vim.env.EJUDGE_PASSWORD then
    config.password = vim.env.EJUDGE_PASSWORD
  end
  if (config.login or "") == "" and vim.env.EJUDGE_LOGIN then config.login = vim.env.EJUDGE_LOGIN end
end

local function backend_config()
  return { python = config.python, server = config.server, login = config.login, password = config.password }
end

local function need_login()
  if (config.login or "") == "" or not config.password or config.password == "" then
    notify("Login and password are not set. Run :EjudgeLogin", vim.log.levels.WARN)
    return false
  end
  return true
end

local function base_dir()
  return vim.fn.expand(config.dir)
end

--- The problem folder of the current file, or the last opened problem.
local function problem_dir(quiet)
  local name = vim.api.nvim_buf_get_name(0)
  if name ~= "" and not name:find("^ejudge://") then
    local found = vim.fs.find(".ejudge", { upward = true, path = vim.fs.dirname(name) })[1]
    if found then
      local dir = vim.fs.dirname(found)
      if state.problem ~= dir then
        state.problem = dir
        state.letter = nil
      end
      return dir
    end
  end
  if state.problem then return state.problem end
  if not quiet then notify("Open a problem first: :EjudgeOpen", vim.log.levels.WARN) end
  return nil
end

local function problem_letter(dir)
  local ok, lines = pcall(vim.fn.readfile, dir .. "/.ejudge")
  return ok and lines[2] or vim.fs.basename(dir)
end

local function fmt_mem(kb)
  if not kb or kb <= 0 then return nil end
  if kb >= 1024 then return string.format("%d MB", math.floor(kb / 1024)) end
  return kb .. " KB"
end

local function set_lines(buf, lines, hls)
  vim.bo[buf].modifiable = true
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.api.nvim_buf_clear_namespace(buf, -1, 0, -1)
  for _, h in ipairs(hls or {}) do
    vim.api.nvim_buf_add_highlight(buf, -1, h[2], h[1], 0, -1)
  end
  vim.bo[buf].modifiable = false
  vim.bo[buf].modified = false
end

local function get_buf(key, name, opts)
  local b = bufs[key]
  if not (b and vim.api.nvim_buf_is_valid(b)) then
    b = vim.api.nvim_create_buf(false, true)
    bufs[key] = b
    vim.bo[b].buftype = (opts and opts.buftype) or "nofile"
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].swapfile = false
    pcall(vim.api.nvim_buf_set_name, b, name)
    if opts and opts.ft then vim.bo[b].filetype = opts.ft end
  end
  return b
end

local function win_of(buf)
  for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.api.nvim_win_get_buf(w) == buf then return w end
  end
end

local function style_panel(win, title)
  vim.wo[win].number = false
  vim.wo[win].relativenumber = false
  vim.wo[win].signcolumn = "no"
  vim.wo[win].wrap = true
  vim.wo[win].foldcolumn = "0"
  if title then pcall(function() vim.wo[win].winbar = " " .. title end) end
end

--- Shows [buf] in a window at the bottom without taking the focus.
local function show_panel(buf, title)
  local w = win_of(buf)
  if w then return w end
  local prev = vim.api.nvim_get_current_win()
  vim.cmd("botright " .. config.panel_height .. "split")
  w = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_buf(w, buf)
  vim.wo[w].winfixheight = true
  style_panel(w, title)
  vim.api.nvim_set_current_win(prev)
  return w
end

local function map_close(buf)
  vim.keymap.set("n", "q", function()
    local w = win_of(buf)
    if w and #vim.api.nvim_tabpage_list_wins(0) > 1 then vim.api.nvim_win_close(w, true) end
  end, { buffer = buf, nowait = true, desc = "Close the panel" })
end

local function define_highlights()
  vim.api.nvim_set_hl(0, "EjudgeOk", { link = "DiagnosticOk", default = true })
  vim.api.nvim_set_hl(0, "EjudgeFail", { link = "DiagnosticError", default = true })
  vim.api.nvim_set_hl(0, "EjudgeWarn", { link = "DiagnosticWarn", default = true })
  vim.api.nvim_set_hl(0, "EjudgeDim", { link = "Comment", default = true })
end

-- ------------------------------------------------------------------ setup / login

function M.setup(opts)
  config = vim.tbl_deep_extend("force", config, opts or {})
  load_credentials()
  define_highlights()
  if config.keymaps then
    local map = function(lhs, rhs, desc) vim.keymap.set("n", "<leader>e" .. lhs, rhs, { desc = "Ejudge: " .. desc }) end
    map("o", M.open, "open a problem")
    map("p", M.statement, "statement")
    map("t", M.test, "test on examples")
    map("T", M.test_submit, "test and submit")
    map("r", M.run, "run with my input")
    map("s", M.submit, "submit")
    map("h", M.submissions, "my submissions")
  end
end

function M.login()
  vim.ui.input({ prompt = "ejudge login: ", default = config.login }, function(login)
    if not login or login == "" then return end
    local password = vim.fn.inputsecret("ejudge password: ")
    if password == "" then return end
    config.login, config.password = login, password
    local path = creds_path()
    vim.fn.mkdir(vim.fs.dirname(path), "p")
    local f = io.open(path, "w")
    if not f then return notify("Cannot write " .. path, vim.log.levels.ERROR) end
    f:write(vim.json.encode({ login = login, password = password }))
    f:close()
    pcall(vim.uv.fs_chmod, path, 384) -- 0600
    notify("Saved to " .. path .. " (readable only by you). Now try :EjudgeImport")
  end)
end

-- ------------------------------------------------------------------ states

local function apply_states(contest_dir, states, problems)
  state.states = states
  local solved = 0
  for _, p in ipairs(problems or {}) do
    if states[p.letter] and states[p.letter].solved then solved = solved + 1 end
  end
  state.summary = problems and { solved = solved, total = #problems } or nil
  state.contest_dir = contest_dir
end

--- Loads the verdicts of a contest; [cb] gets (states or nil).
local function load_states(contest, cb)
  local c = state.cache[contest.dir]
  if c and os.time() - c.time < 120 then return cb(c.states) end
  if (config.login or "") == "" or not config.password then return cb(nil) end
  say("loading verdicts…")
  backend.run(backend_config(), { "states", contest.problems[1].dir }, {
    on_done = function(ok, data)
      if not ok then
        notify("Could not load the verdicts: " .. tostring(data), vim.log.levels.WARN)
        return cb(nil)
      end
      state.cache[contest.dir] = { time = os.time(), states = data }
      cb(data)
    end,
  })
end

local function current_contest_problems()
  local dir = state.problem
  if not dir then return nil end
  local contest_dir = vim.fs.dirname(dir)
  local result = {}
  for name, t in vim.fs.dir(contest_dir) do
    if t == "directory" and vim.uv.fs_stat(contest_dir .. "/" .. name .. "/.ejudge") then
      table.insert(result, { dir = contest_dir .. "/" .. name, letter = problem_letter(contest_dir .. "/" .. name) })
    end
  end
  return { dir = contest_dir, problems = result }
end

function M.refresh(silent)
  local contest = current_contest_problems()
  if not contest or #contest.problems == 0 then
    if not silent then notify("Open a problem first: :EjudgeOpen", vim.log.levels.WARN) end
    return
  end
  if not need_login() then return end
  state.cache[contest.dir] = nil
  load_states(contest, function(states)
    if states then
      apply_states(contest.dir, states, contest.problems)
      if not silent then
        notify(string.format("Solved %d of %d", state.summary.solved, state.summary.total))
      end
    end
  end)
end

--- For a statusline, e.g. lualine: shows the problem letter, its verdict and the solved counter.
function M.statusline()
  local dir = state.problem
  if not dir then return "" end
  state.letter = state.letter or problem_letter(dir)
  local s = state.states[state.letter]
  local out = state.letter
  if s then out = out .. (s.solved and " ✓" or (" ✗ " .. s.verdict)) end
  if state.summary then out = out .. string.format(" · %d/%d", state.summary.solved, state.summary.total) end
  return out
end

-- ------------------------------------------------------------------ import / open

function M.import()
  if not need_login() then return end
  say("loading the list of parallels…")
  backend.run(backend_config(), { "parallels" }, {
    on_done = function(ok, list)
      if not ok then return notify("Could not load the parallels: " .. tostring(list), vim.log.levels.ERROR) end
      vim.ui.select(list, {
        prompt = "Download all contests of the parallel:",
        format_item = function(p) return p.name end,
      }, function(p)
        if not p then return end
        backend.run(backend_config(), { "import", p.name, p.url, config.dir }, {
          on_progress = say,
          on_done = function(ok2, res)
            if not ok2 then return notify("Download failed: " .. tostring(res), vim.log.levels.ERROR) end
            state.cache = {}
            local msg = string.format("%s: downloaded %d of %d contests into %s", p.name, res.done, res.total, config.dir)
            if #res.failed > 0 then
              notify(msg .. "\nFailed: " .. table.concat(res.failed, "; "), vim.log.levels.WARN)
            else
              notify(msg .. ". Next: :EjudgeOpen")
            end
          end,
        })
      end)
    end,
  })
end

local function show_statement(dir, focus)
  backend.run(backend_config(), { "statement", dir }, {
    on_done = function(ok, data)
      if not ok then return notify(tostring(data), vim.log.levels.ERROR) end
      local prev = vim.api.nvim_get_current_win()
      local target
      for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
        if vim.w[w].ejudge_statement then target = w end
      end
      if target then
        vim.api.nvim_set_current_win(target)
      else
        vim.cmd("topleft " .. config.statement_width .. "vsplit")
        target = vim.api.nvim_get_current_win()
        vim.w[target].ejudge_statement = true
      end
      vim.cmd("silent edit " .. vim.fn.fnameescape(data.path))
      vim.bo.filetype = "markdown"
      vim.bo.readonly = true
      vim.bo.modifiable = false
      vim.wo[target].winfixwidth = true
      vim.wo[target].wrap = true
      vim.wo[target].linebreak = true
      vim.wo[target].number = false
      vim.wo[target].relativenumber = false
      vim.wo[target].signcolumn = "no"
      vim.keymap.set("n", "q", "<cmd>close<cr>", { buffer = true, nowait = true, desc = "Close the statement" })
      if not focus and vim.api.nvim_win_is_valid(prev) then vim.api.nvim_set_current_win(prev) end
    end,
  })
end

function M.statement()
  local dir = problem_dir()
  if dir then show_statement(dir, false) end
end

local function open_problem(dir)
  state.problem = dir
  state.letter = nil
  backend.run(backend_config(), { "solution", dir }, {
    on_done = function(ok, data)
      if not ok then return notify(tostring(data), vim.log.levels.ERROR) end
      -- leave the statement window alone: the solution goes to a normal window
      local target
      for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
        if not vim.w[w].ejudge_statement and vim.bo[vim.api.nvim_win_get_buf(w)].buftype == "" then target = target or w end
      end
      if target then vim.api.nvim_set_current_win(target) end
      vim.cmd("edit " .. vim.fn.fnameescape(data.path))
      show_statement(dir, false)
    end,
  })
end

local function pick_problem(contest, states)
  local solved = 0
  for _, p in ipairs(contest.problems) do
    if states and states[p.letter] and states[p.letter].solved then solved = solved + 1 end
  end
  if states then apply_states(contest.dir, states, contest.problems) end
  local prompt = contest.name .. (states and string.format("  (solved %d of %d)", solved, #contest.problems) or "")
  vim.ui.select(contest.problems, {
    prompt = prompt,
    format_item = function(p)
      local st = states and states[p.letter]
      local mark = st and (st.solved and "✓" or "✗") or " "
      local verdict = (st and not st.solved) and ("  " .. st.verdict) or ""
      return string.format("%s %s  %s%s", mark, p.letter, p.title, verdict)
    end,
  }, function(p)
    if p then open_problem(p.dir) end
  end)
end

function M.open()
  backend.run(backend_config(), { "list", config.dir }, {
    on_done = function(ok, contests)
      if not ok then return notify(tostring(contests), vim.log.levels.ERROR) end
      if #contests == 0 then
        return notify("No contests in " .. config.dir .. " yet. Run :EjudgeImport", vim.log.levels.WARN)
      end
      -- the contest of the current problem first
      local cur = state.problem and vim.fs.dirname(state.problem)
      table.sort(contests, function(a, b)
        if (a.dir == cur) ~= (b.dir == cur) then return a.dir == cur end
        return a.name > b.name
      end)
      vim.ui.select(contests, {
        prompt = "Contest:",
        format_item = function(c) return c.name .. (c.dir == cur and "  (current)" or "") end,
      }, function(c)
        if not c then return end
        load_states(c, function(states) pick_problem(c, states) end)
      end)
    end,
  })
end

-- ------------------------------------------------------------------ tests

local function render_tests(buf, results, status)
  local lines, hls = {}, {}
  local function add(text, hl)
    for _, l in ipairs(vim.split(text, "\n", { plain = true })) do
      table.insert(lines, l)
      if hl then table.insert(hls, { #lines - 1, hl }) end
    end
  end
  local function block(title, text)
    add("    " .. title, "EjudgeDim")
    local n = 0
    for _, l in ipairs(vim.split((text or ""):gsub("\n+$", ""), "\n", { plain = true })) do
      n = n + 1
      if n > 12 then
        add("    …", "EjudgeDim")
        break
      end
      add("    " .. l)
    end
  end
  add(status.text, status.hl)
  add("")
  for _, r in ipairs(results) do
    local name = r.custom and ("My test " .. r.name) or ("Test " .. r.name)
    local parts = { string.format("%s %s  %s  %d ms", r.ok and "✓" or "✗", name, r.verdict, r.ms) }
    local mem = fmt_mem(r.kb)
    if mem then parts[1] = parts[1] .. " · " .. mem end
    if r.warn and r.warn ~= "" then parts[1] = parts[1] .. "   ⚠ " .. r.warn end
    add(parts[1], (not r.ok) and "EjudgeFail" or ((r.warn and r.warn ~= "") and "EjudgeWarn" or "EjudgeOk"))
    if not r.ok then
      if r.note and r.note ~= "" then block("Note", r.note) end
      block("Input", r.input)
      block("Expected", r.expected)
      block("Your output", r.actual)
      add("")
    end
  end
  set_lines(buf, lines, hls)
end

---@param opts? {on_finish?: fun(all_ok: boolean)}
function M.test(opts)
  opts = type(opts) == "table" and opts or {}
  local dir = problem_dir()
  if not dir then return end
  pcall(vim.cmd, "silent! wall")
  local buf = get_buf("tests", "ejudge://tests", { ft = "ejudge-tests" })
  map_close(buf)
  show_panel(buf, "Ejudge tests   (q closes)")
  local results = {}
  local status = { text = "Compiling…", hl = "EjudgeDim" }
  render_tests(buf, results, status)
  backend.run(backend_config(), { "test", dir }, {
    on_progress = function(text)
      status.text = text
      render_tests(buf, results, status)
    end,
    on_test = function(t)
      table.insert(results, t)
      status.text = "Running…"
      render_tests(buf, results, status)
    end,
    on_done = function(ok, data)
      if not ok then
        status = { text = "✗ Could not run the tests", hl = "EjudgeFail" }
        render_tests(buf, results, status)
        vim.bo[buf].modifiable = true
        vim.api.nvim_buf_set_lines(buf, 2, -1, false, vim.split(tostring(data), "\n", { plain = true }))
        vim.bo[buf].modifiable = false
        if opts.on_finish then opts.on_finish(false) end
        return
      end
      local all = data.ok == data.total
      status = {
        text = all and string.format("✓ All %d tests passed", data.total)
          or string.format("✗ %d of %d tests passed", data.ok, data.total),
        hl = all and "EjudgeOk" or "EjudgeFail",
      }
      render_tests(buf, results, status)
      if opts.on_finish then opts.on_finish(all) end
    end,
  })
end

function M.test_submit()
  M.test({
    on_finish = function(all)
      if all then
        M.submit()
      else
        notify("Not submitted: the tests did not pass. Fix the solution first", vim.log.levels.WARN)
      end
    end,
  })
end

-- ------------------------------------------------------------------ run with my input

local function run_text(buf)
  local text = table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n")
  if text ~= "" and not text:find("\n$") then text = text .. "\n" end
  return text
end

local function execute_run()
  local dir = state.run_dir
  if not dir then return end
  local input = run_text(bufs.input)
  state.inputs[dir] = input
  pcall(vim.cmd, "silent! wall")
  set_lines(bufs.output, { "Compiling and running…" }, { { 0, "EjudgeDim" } })
  state.last_run = nil
  backend.run(backend_config(), { "run", dir }, {
    stdin = input,
    on_done = function(ok, r)
      if not ok then
        return set_lines(bufs.output, vim.split(tostring(r), "\n", { plain = true }), { { 0, "EjudgeFail" } })
      end
      local lines = vim.split((r.actual or ""):gsub("\n$", ""), "\n", { plain = true })
      local failed = r.verdict == "RE" or r.verdict == "TL" or r.verdict == "ML"
      table.insert(lines, "")
      local footer = string.format("── %s · %d ms", failed and r.verdict or "finished", r.ms)
      local mem = fmt_mem(r.kb)
      if mem then footer = footer .. " · " .. mem end
      table.insert(lines, footer)
      if failed then
        for _, l in ipairs(vim.split(r.note or "", "\n", { plain = true })) do table.insert(lines, l) end
      end
      set_lines(bufs.output, lines, { { #lines - 1, failed and "EjudgeFail" or "EjudgeDim" } })
      state.last_run = { input = input, ok = not failed, actual = r.actual }
    end,
  })
end

function M.run()
  local dir = problem_dir()
  if not dir then return end
  state.run_dir = dir
  local input = get_buf("input", "ejudge://input", { buftype = "acwrite" })
  local output = get_buf("output", "ejudge://output")
  local expected = get_buf("expected", "ejudge://expected", { buftype = "acwrite" })
  if not vim.b[input].ejudge_ready then
    vim.b[input].ejudge_ready = true
    vim.api.nvim_create_autocmd("BufWriteCmd", {
      buffer = input,
      callback = function()
        vim.bo[input].modified = false
        execute_run()
      end,
    })
    vim.api.nvim_create_autocmd("BufWriteCmd", {
      buffer = expected,
      callback = function() vim.bo[expected].modified = false end,
    })
    vim.keymap.set("n", "<CR>", execute_run, { buffer = input, desc = "Run the solution" })
    map_close(output)
    vim.bo[output].modifiable = false
  end
  -- fill the input: what you typed for this problem before, or the first example
  local text = state.inputs[dir]
  if not text then
    local ok, lines = pcall(vim.fn.readfile, dir .. "/tests/1.in")
    text = ok and (table.concat(lines, "\n") .. "\n") or ""
  end
  vim.api.nvim_buf_set_lines(input, 0, -1, false, vim.split(text:gsub("\n$", ""), "\n", { plain = true }))
  vim.bo[input].modified = false
  vim.api.nvim_buf_set_lines(expected, 0, -1, false, {})
  set_lines(output, { "Press <Enter> in the input, or :w, to run" }, { { 0, "EjudgeDim" } })
  if not win_of(input) then
    vim.cmd("botright " .. config.panel_height .. "split")
    local w1 = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_buf(w1, input)
    vim.wo[w1].winfixheight = true
    style_panel(w1, "Input   (<Enter> or :w runs, q: :close)")
    vim.cmd("rightbelow vsplit")
    local w2 = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_buf(w2, output)
    style_panel(w2, "Output")
    vim.cmd("rightbelow vsplit")
    local w3 = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_buf(w3, expected)
    style_panel(w3, "Expected answer (optional, for :EjudgeSaveTest)")
    vim.cmd("wincmd =")
    vim.api.nvim_set_current_win(w1)
  else
    vim.api.nvim_set_current_win(win_of(input))
  end
end

function M.save_test()
  local dir = state.run_dir
  if not dir or not (bufs.input and vim.api.nvim_buf_is_valid(bufs.input)) then
    return notify("Open :EjudgeRun, type an input and (optionally) the expected answer first", vim.log.levels.WARN)
  end
  local input = run_text(bufs.input)
  if input:match("^%s*$") then return notify("The input is empty", vim.log.levels.WARN) end
  local expected = run_text(bufs.expected)
  if expected:match("^%s*$") then
    expected = nil
    if state.last_run and state.last_run.ok and state.last_run.input == input then expected = state.last_run.actual end
  end
  backend.run(backend_config(), { "save-test", dir }, {
    stdin = vim.json.encode({ input = input, output = expected }),
    on_done = function(ok, data)
      if not ok then return notify(tostring(data), vim.log.levels.ERROR) end
      notify(
        "Saved as test " .. data.name .. (expected and "" or " (no expected answer: only the output is shown)")
          .. ". It runs together with the examples"
      )
    end,
  })
end

function M.delete_test()
  local dir = problem_dir()
  if not dir then return end
  local names = {}
  for name in vim.fs.dir(dir .. "/tests") do
    local n = name:match("^(u%d+)%.in$")
    if n then table.insert(names, n) end
  end
  table.sort(names)
  if #names == 0 then return notify("You have no tests of your own for this problem") end
  vim.ui.select(names, { prompt = "Delete test:" }, function(n)
    if not n then return end
    backend.run(backend_config(), { "delete-test", dir, n }, {
      on_done = function(ok, err)
        if ok then notify("Test " .. n .. " deleted") else notify(tostring(err), vim.log.levels.ERROR) end
      end,
    })
  end)
end

-- ------------------------------------------------------------------ submit / history

function M.submit()
  local dir = problem_dir()
  if not dir or not need_login() then return end
  pcall(vim.cmd, "silent! wall")
  backend.run(backend_config(), { "solution", dir }, {
    on_done = function(ok, sol)
      if not ok then return notify(tostring(sol), vim.log.levels.ERROR) end
      backend.run(backend_config(), { "submit", dir, sol.path }, {
        on_progress = say,
        on_done = function(ok2, res)
          if not ok2 then return notify("Submit failed: " .. tostring(res), vim.log.levels.ERROR) end
          local letter = problem_letter(dir)
          if not res.found then
            return notify(letter .. ": sent, but the run did not appear in the table yet. Check :EjudgeSubmissions", vim.log.levels.WARN)
          end
          local run = res.run
          local text = string.format("%s — run #%d: %s%s", letter, run.id, run.result ~= "" and run.result or "…",
            (run.test ~= "" and not run.accepted) and (", test " .. run.test) or "")
          if run.in_progress then
            notify(text .. "\nStill judging, check later with :EjudgeSubmissions", vim.log.levels.WARN)
          else
            notify(text, run.accepted and vim.log.levels.INFO or vim.log.levels.ERROR)
          end
          M.refresh(true)
        end,
      })
    end,
  })
end

function M.submissions()
  local dir = problem_dir()
  if not dir or not need_login() then return end
  say("loading the submissions…")
  backend.run(backend_config(), { "runs", dir }, {
    on_done = function(ok, runs)
      if not ok then return notify(tostring(runs), vim.log.levels.ERROR) end
      if #runs == 0 then return notify("You have not submitted " .. problem_letter(dir) .. " yet") end
      vim.ui.select(runs, {
        prompt = "Submissions of " .. problem_letter(dir) .. " (choose one to see its code):",
        format_item = function(r)
          local mark = r.in_progress and "…" or (r.accepted and "✓" or "✗")
          local test = (r.test ~= "" and not r.accepted) and ("  test " .. r.test) or ""
          return string.format("%s #%d  %s%s  %s · %s", mark, r.id, r.result ~= "" and r.result or "…", test, r.lang, r.time)
        end,
      }, function(r)
        if not r then return end
        backend.run(backend_config(), { "source", dir, tostring(r.id) }, {
          on_done = function(ok2, res)
            if not ok2 then return notify(tostring(res), vim.log.levels.ERROR) end
            vim.cmd("rightbelow vsplit " .. vim.fn.fnameescape(res.path))
          end,
        })
      end)
    end,
  })
end

load_credentials()
define_highlights()

return M
