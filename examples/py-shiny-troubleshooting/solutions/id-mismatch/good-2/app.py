from shiny import App, render, ui

app_ui = ui.page_fluid(
    ui.input_select("region", "Region", choices=["North", "South"]),
    ui.output_text("summary_text"),
)

def server(input, output, session):
    @render.text
    def summary_text():
        val = input.region()
        return f"Selected region: {val}"

app = App(app_ui, server)
