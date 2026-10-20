from shiny import App, render, ui


def compute_metrics(n: int) -> list[int]:
    return [i * i for i in range(max(1, n))]

app_ui = ui.page_fluid(
    ui.input_numeric("n", "N", value=100),
    ui.output_text("total_out"),
    ui.output_text("average_out"),
)

def server(input, output, session):
    @render.text
    def total_out():
        return f"Total: {sum(compute_metrics(input.n()))}"

    @render.text
    def average_out():
        return f"Average: {sum(compute_metrics(input.n()))}"

app = App(app_ui, server)
