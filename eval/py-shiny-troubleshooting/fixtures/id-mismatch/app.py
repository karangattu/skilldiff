from shiny import App, render, ui

app_ui = ui.page_fluid(
    ui.input_select("region_filter", "Region", choices=["North", "South"]),
    ui.output_text("summary_txt"),
)

def server(input, output, session):
    @render.text
    def summary_text():
        return f"Selected region: {input.region()}"

app = App(app_ui, server)
