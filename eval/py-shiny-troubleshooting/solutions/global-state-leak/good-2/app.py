from shiny import App, reactive, render, ui

app_ui = ui.page_fluid(
    ui.input_text("note", "Note"),
    ui.input_action_button("save", "Save"),
    ui.output_text("notes_view"),
)

def server(input, output, session):
    session_notes = reactive.Value(())

    @reactive.effect
    @reactive.event(input.save)
    def _():
        session_notes.set((*session_notes.get(), input.note()))

    @render.text
    def notes_view():
        return ", ".join(session_notes())

app = App(app_ui, server)
