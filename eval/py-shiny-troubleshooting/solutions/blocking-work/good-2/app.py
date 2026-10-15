from shiny import App, reactive, render, ui

def compute_metrics(n: int) -> list[int]:
    return [i * i for i in range(max(1, n))]

app_ui = ui.page_fluid(
    ui.input_numeric("n", "N", value=100),
    ui.output_text("total_out"),
    ui.output_text("average_out"),
)

def server(input, output, session):
    @reactive.Calc
    def cached_values():
        return compute_metrics(int(input.n()))

    @render.text
    def total_out():
        return f"Total: {sum(cached_values())}"

    @render.text
    def average_out():
        data = cached_values()
        return f"Average: {sum(data) / len(data):.2f}"

app = App(app_ui, server)
