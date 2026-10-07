if vim.g.loaded_ejudge then return end
vim.g.loaded_ejudge = 1

local commands = {
  EjudgeLogin = { "login", "Save the ejudge login and password" },
  EjudgeImport = { "import", "Download all contests of a parallel" },
  EjudgeOpen = { "open", "Pick a contest and a problem" },
  EjudgeStatement = { "statement", "Show the statement of the current problem" },
  EjudgeTest = { "test", "Compile and run the examples and your own tests" },
  EjudgeTestSubmit = { "test_submit", "Run the tests and submit if all of them pass" },
  EjudgeRun = { "run", "Run the solution on your own input" },
  EjudgeSaveTest = { "save_test", "Save the input of :EjudgeRun as your own test" },
  EjudgeDeleteTest = { "delete_test", "Delete one of your own tests" },
  EjudgeSubmit = { "submit", "Send the current solution to the server" },
  EjudgeSubmissions = { "submissions", "Verdicts and code of your submissions of the problem" },
  EjudgeRefresh = { "refresh", "Reload the verdicts of the contest" },
}

for name, def in pairs(commands) do
  vim.api.nvim_create_user_command(name, function()
    require("ejudge")[def[1]]()
  end, { desc = def[2] })
end
